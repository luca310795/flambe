from typing import Optional, Dict, List, Any
import os
import copy
import getpass
import logging

import ray

from flambe.runner import Environment
from flambe.logging import TrialLogging
from flambe.compile import Registrable, YAMLLoadType, Schema
from flambe.search.trial import Trial
from flambe.search.protocol import Searchable
from flambe.search.checkpoint import Checkpoint
from flambe.search.algorithm import Algorithm, GridSearch


@ray.remote
class RayAdapter:
    """Perform computation of a task."""

    def __init__(self,
                 schema: Schema,
                 checkpoint: Checkpoint,
                 environment: Environment) -> None:
        """Initialize the Trial."""
        self.schema = schema
        self.searchable: Optional[Searchable] = None
        self.checkpoint = checkpoint
        self.environment = environment
        self.trial_logging = TrialLogging(checkpoint.path)
        self.trial_logging.setup()

    def step(self) -> Any:
        """Run a step of the Trial"""
        if self.searchable is None:
            self.searchable = self.schema()
            if not isinstance(self.searchable, Searchable):
                return False, None
        continue_ = self.searchable.step(self.environment)
        metric = self.searchable.metric(self.environment)
        self.checkpoint.set(self.searchable)
        return continue_, metric

    def __del__(self):
        self.trial_logging.teardown()


class Search(Registrable):
    """Implement a hyperparameter search over any schema.

    Use a Search to construct a hyperparameter search over any
    objects. Takes as input a schema to search, and algorithm,
    and resources to allocate per variant.

    Example
    -------

    >>> schema = Schema(cls, arg1=uniform(0, 1), arg2=choice('a', 'b'))
    >>> search = Search(schema, algorithm=Hyperband())
    >>> search.run()

    """

    def __init__(self,
                 schema: Schema,
                 algorithm: Optional[Algorithm] = None,
                 cpus_per_trial: int = 1,
                 gpus_per_trial: int = 0,
                 refresh_waitime: float = 30.0) -> None:
        """Initialize a hyperparameter search.

        Parameters
        ----------
        schema : Schema[Task]
            A schema of the callable or Task to search
        algorithm : Algorithm
            The hyperparameter search algorithm
        cpus_per_trial : int
            The number of cpu's to allocate per trial
        gpus_per_trial : int
            The number of gpu's to allocate per trial
        output_path: str, optional
            An output path for this search
        refresh_waitime : float, optional
            The minimum amount of time to wait before a refresh.
            Defaults ``1`` seconds.

        """
        self.n_cpus = cpus_per_trial
        self.n_gpus = gpus_per_trial
        self.refresh_waitime = refresh_waitime

        self.schema = schema
        self.algorithm = GridSearch() if algorithm is None else algorithm

    @classmethod
    def yaml_load_type(cls) -> YAMLLoadType:
        return YAMLLoadType.KWARGS

    def run(self, env: Optional[Environment] = None) -> Dict[str, Dict[str, Any]]:
        """Execute the search.

        Parameters
        ----------
        env : Environment, optional
            An environment object.

        Returns
        -------
        Dict[str, Dict[str, Any]]
            A dictionary pointing from trial name to sub-dicionary
            containing the keys 'trial' and 'checkpoint' pointing to
            a Trial and a Checkpoint object respecitvely.

        """
        env = env if env is not None else Environment()

        if not ray.is_initialized():
            ray.init(address="auto", local_mode=env.debug)

        if not env.debug:
            total_resources = ray.cluster_resources()
            if self.n_cpus > 0 and self.n_cpus > total_resources['CPU']:
                raise ValueError("# of CPUs required per trial is larger than the total available.")
            elif self.n_gpus > 0 and self.n_gpus > total_resources['GPU']:
                raise ValueError("# of CPUs required per trial is larger than the total available.")

        search_space = self.schema.extract_search_space().items()
        self.algorithm.initialize(dict((".".join(k), v) for k, v in search_space))  # type: ignore
        running: List[int] = []
        finished: List[int] = []

        trials: Dict[str, Trial] = dict()
        state: Dict[str, Dict[str, Any]] = dict()
        object_id_to_trial_id: Dict[str, str] = dict()
        trial_id_to_object_id: Dict[str, str] = dict()

        while not self.algorithm.is_done():

            # Get all the current object ids running
            finished = []
            if running:
                # TODO change if needed for failures to come up properly
                # finished, running = ray.get(running, timeout=self.refresh_waitime)
                finished, running = ray.wait(running, timeout=self.refresh_waitime)

            # Process finished trials
            for object_id in finished:
                trial_id = object_id_to_trial_id[str(object_id)]
                trial = trials[trial_id]
                try:
                    _continue, metric = ray.get(object_id)
                    if _continue:
                        trial.set_metric(metric)
                        trial.set_has_result()
                    else:
                        trial.set_terminated()
                except Exception as e:
                    trial.set_error()
                    if env.debug:
                        logging.warn(str(e))
                    logging.warn(f"Trial {trial_id} failed.")

            # Compute maximum number of trials to create
            if env.debug:
                max_queries = int(all(t.is_terminated() or t.is_error() for t in trials.values()))
            else:
                current_resources = ray.available_resources()
                max_queries = current_resources.get('CPU', 0) // self.n_cpus
                if self.n_gpus:
                    n_possible_gpu = current_resources.get('GPU', 0) // self.n_gpus
                    max_queries = min(max_queries, n_possible_gpu)

            # Update the algorithm and get new trials
            trials = self.algorithm.update(trials, maximum=max_queries)

            # Update based on trial status
            for trial_id, trial in trials.items():
                # Handle creation and termination
                if trial.is_paused() or trial.is_running():
                    continue
                elif (trial.is_error() or trial.is_terminated()) and 'actor' in state[trial_id]:
                    del state[trial_id]['actor']
                    continue
                elif trial.is_created():
                    space = dict((tuple(k.split('.')), v) for k, v in trial.parameters.items())
                    schema_copy = copy.deepcopy(self.schema)
                    schema_copy.set_from_search_space(space)

                    # Update state
                    trial.set_resume()
                    state[trial_id] = dict()
                    state[trial_id]['schema'] = schema_copy
                    trial_path = os.path.join(env.output_path, trial_id)
                    checkpoint = Checkpoint(
                        path=trial_path,
                        host=env.head_node_ip,
                        user=getpass.getuser()
                    )
                    state[trial_id]['checkpoint'] = checkpoint
                    state[trial_id]['actor'] = RayAdapter.options(  # type: ignore
                        num_cpus=self.n_cpus,
                        num_gpus=self.n_gpus
                    ).remote(
                        schema=schema_copy,
                        checkpoint=checkpoint,
                        environment=env.clone(output_path=trial_path)
                    )

                # Launch created and resumed
                if trial.is_resuming():
                    object_id = state[trial_id]['actor'].step.remote()
                    object_id_to_trial_id[str(object_id)] = trial_id
                    trial_id_to_object_id[trial_id] = str(object_id)
                    running.append(object_id)
                    trial.set_running()

        # Construct result output
        results = dict()
        for trial_id, trial in trials.items():
            results[trial_id] = ({
                'schema': state[trial_id].get('schema'),
                'checkpoint': state[trial_id].get('checkpoint', None),
                'error': trial.is_error(),
                'metric': trial.best_metric,
                # The ray object id is guarenteed to be unique
                'var_id': str(trial_id_to_object_id[trial_id])
            })
        return results
