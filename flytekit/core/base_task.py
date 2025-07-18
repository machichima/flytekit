"""
# flytekit.core.base_task

This module provides the core task-related functionality in Flytekit.

## Core Components

### kwtypes
Utility for creating keyword type annotations for tasks.

### PythonTask
Base class for Python-based task implementations.

### Task
The base class for all Flyte tasks.

### TaskResolverMixin
Mixin class that helps resolve a task implementation.

### IgnoreOutputs
Exception that can be raised to ignore task outputs.

"""

import asyncio
import collections
import datetime
import warnings
from abc import abstractmethod
from base64 import b64encode
from dataclasses import dataclass
from typing import (
    Any,
    Coroutine,
    Dict,
    Generic,
    List,
    Optional,
    OrderedDict,
    Tuple,
    Type,
    TypeVar,
    Union,
    cast,
)

from flyteidl.core import artifact_id_pb2 as art_id
from flyteidl.core import tasks_pb2

from flytekit.configuration import LocalConfig, SerializationSettings
from flytekit.core.artifact_utils import (
    idl_partitions_from_dict,
    idl_time_partition_from_datetime,
)
from flytekit.core.context_manager import (
    ExecutionParameters,
    ExecutionState,
    FlyteContext,
    FlyteContextManager,
    FlyteEntities,
)
from flytekit.core.interface import Interface, transform_interface_to_typed_interface
from flytekit.core.local_cache import LocalTaskCache
from flytekit.core.promise import (
    Promise,
    VoidPromise,
    create_and_link_node,
    create_task_output,
    extract_obj_name,
    flyte_entity_call_handler,
    translate_inputs_to_literals,
)
from flytekit.core.tracker import TrackedInstance
from flytekit.core.type_engine import TypeEngine, TypeTransformerFailedError
from flytekit.core.utils import timeit
from flytekit.deck import DeckField
from flytekit.exceptions.system import (
    FlyteDownloadDataException,
    FlyteNonRecoverableSystemException,
    FlyteUploadDataException,
)
from flytekit.exceptions.user import FlyteUserRuntimeException
from flytekit.loggers import logger
from flytekit.models import dynamic_job as _dynamic_job
from flytekit.models import interface as _interface_models
from flytekit.models import literals as _literal_models
from flytekit.models import task as _task_model
from flytekit.models.core import workflow as _workflow_model
from flytekit.models.documentation import Description, Documentation
from flytekit.models.interface import Variable
from flytekit.models.security import SecurityContext
from flytekit.utils.asyn import run_sync

DYNAMIC_PARTITIONS = "_uap"
MODEL_CARD = "_ucm"
DATA_CARD = "_ucd"
UNSET_CARD = "_uc"


def kwtypes(**kwargs) -> OrderedDict[str, Type]:
    """
    This is a small helper function to convert the keyword arguments to an OrderedDict of types.

    ```python
    kwtypes(a=int, b=str)
    ```
    """
    d = collections.OrderedDict()
    for k, v in kwargs.items():
        d[k] = v
    return d


@dataclass
class TaskMetadata(object):
    """
    Metadata for a Task. Things like retries and whether or not caching is turned on, and cache version are specified
    here.

    See the :std:ref:`IDL <idl:protos/docs/core/core:taskmetadata>` for the protobuf definition.

    Attributes:
        cache (bool): Indicates if caching should be enabled. See :std:ref:`Caching <cookbook:caching>`.
        cache_serialize (bool): Indicates if identical (i.e. same inputs) instances of this task should be executed in serial when caching is enabled. See :std:ref:`Caching <cookbook:caching>`.
        cache_version (str): Version to be used for the cached value.
        cache_ignore_input_vars (Tuple[str, ...]): Input variables that should not be included when calculating hash for cache.
        interruptible (Optional[bool]): Indicates that this task can be interrupted and/or scheduled on nodes with lower QoS guarantees that can include pre-emption.
        deprecated (str): Can be used to provide a warning message for a deprecated task. An absence or empty string indicates that the task is active and not deprecated.
        retries (int): for retries=n; n > 0, on failures of this task, the task will be retried at-least n number of times.
        timeout (Optional[Union[datetime.timedelta, int]]): The maximum duration for which one execution of this task should run. The execution will be terminated if the runtime exceeds this timeout.
        pod_template_name (Optional[str]): The name of an existing PodTemplate resource in the cluster which will be used for this task.
        generates_deck (bool): Indicates whether the task will generate a Deck URI.
        is_eager (bool): Indicates whether the task should be treated as eager.
        labels (Optional[dict[str, str]]): Labels to be applied to the task resource.
        annotations (Optional[dict[str, str]]): Annotations to be applied to the task resource.
    """

    cache: bool = False
    cache_serialize: bool = False
    cache_version: str = ""
    cache_ignore_input_vars: Tuple[str, ...] = ()
    interruptible: Optional[bool] = None
    deprecated: str = ""
    retries: int = 0
    timeout: Optional[Union[datetime.timedelta, int]] = None
    pod_template_name: Optional[str] = None
    generates_deck: bool = False
    is_eager: bool = False
    labels: Optional[dict[str, str]] = None
    annotations: Optional[dict[str, str]] = None

    def __post_init__(self):
        if self.timeout:
            if isinstance(self.timeout, int):
                self.timeout = datetime.timedelta(seconds=self.timeout)
            elif not isinstance(self.timeout, datetime.timedelta):
                raise ValueError("timeout should be duration represented as either a datetime.timedelta or int seconds")
        if self.cache and not self.cache_version:
            raise ValueError("Caching is enabled ``cache=True`` but ``cache_version`` is not set.")
        if self.cache_serialize and not self.cache:
            raise ValueError("Cache serialize is enabled ``cache_serialize=True`` but ``cache`` is not enabled.")
        if self.cache_ignore_input_vars and not self.cache:
            raise ValueError(
                f"Cache ignore input vars are specified ``cache_ignore_input_vars={self.cache_ignore_input_vars}`` but ``cache`` is not enabled."
            )

    @property
    def retry_strategy(self) -> _literal_models.RetryStrategy:
        return _literal_models.RetryStrategy(self.retries)

    def to_taskmetadata_model(self) -> _task_model.TaskMetadata:
        """
        Converts to _task_model.TaskMetadata
        """
        from flytekit import __version__

        return _task_model.TaskMetadata(
            discoverable=self.cache,
            runtime=_task_model.RuntimeMetadata(
                _task_model.RuntimeMetadata.RuntimeType.FLYTE_SDK, __version__, "python"
            ),
            timeout=self.timeout,
            retries=self.retry_strategy,
            interruptible=self.interruptible,
            discovery_version=self.cache_version,
            deprecated_error_message=self.deprecated,
            cache_serializable=self.cache_serialize,
            generates_deck=self.generates_deck,
            pod_template_name=self.pod_template_name,
            cache_ignore_input_vars=self.cache_ignore_input_vars,
            is_eager=self.is_eager,
            k8s_object_metadata=_task_model.K8sObjectMetadata(
                labels=self.labels,
                annotations=self.annotations,
            ),
        )


class IgnoreOutputs(Exception):
    """
    This exception should be used to indicate that the outputs generated by this can be safely ignored.
    This is useful in case of distributed training or peer-to-peer parallel algorithms.
    """

    pass


class Task(object):
    """
    The base of all Tasks in flytekit. This task is closest to the FlyteIDL TaskTemplate and captures information in
    FlyteIDL specification and does not have python native interfaces associated. Refer to the derived classes for
    examples of how to extend this class.
    """

    def __init__(
        self,
        task_type: str,
        name: str,
        interface: _interface_models.TypedInterface,
        metadata: Optional[TaskMetadata] = None,
        task_type_version=0,
        security_ctx: Optional[SecurityContext] = None,
        docs: Optional[Documentation] = None,
        **kwargs,
    ):
        self._task_type = task_type
        self._name = name
        self._interface = interface
        self._metadata = metadata if metadata else TaskMetadata()
        self._task_type_version = task_type_version
        self._security_ctx = security_ctx
        self._docs = docs

        FlyteEntities.entities.append(self)

    @property
    def interface(self) -> _interface_models.TypedInterface:
        return self._interface

    @property
    def metadata(self) -> TaskMetadata:
        return self._metadata

    @property
    def name(self) -> str:
        return self._name

    @property
    def task_type(self) -> str:
        return self._task_type

    @property
    def python_interface(self) -> Optional[Interface]:
        return None

    @property
    def task_type_version(self) -> int:
        return self._task_type_version

    @property
    def security_context(self) -> SecurityContext:
        return self._security_ctx

    @property
    def docs(self) -> Documentation:
        return self._docs

    def get_type_for_input_var(self, k: str, v: Any) -> type:
        """
        Returns the python native type for the given input variable
        # TODO we could use literal type to determine this
        """
        return type(v)

    def get_type_for_output_var(self, k: str, v: Any) -> type:
        """
        Returns the python native type for the given output variable
        # TODO we could use literal type to determine this
        """
        return type(v)

    def get_input_types(self) -> Optional[Dict[str, type]]:
        """
        Returns python native types for inputs. In case this is not a python native task (base class) and hence
        returns a None. we could deduce the type from literal types, but that is not a required exercise
        # TODO we could use literal type to determine this
        """
        return None

    def local_execute(
        self, ctx: FlyteContext, **kwargs
    ) -> Union[Tuple[Promise], Promise, VoidPromise, Coroutine, None]:
        """
        This function is used only in the local execution path and is responsible for calling dispatch execute.
        Use this function when calling a task with native values (or Promises containing Flyte literals derived from
        Python native values).
        """
        # Unwrap the kwargs values. After this, we essentially have a LiteralMap
        # The reason why we need to do this is because the inputs during local execute can be of 2 types
        #  - Promises or native constants
        #  Promises as essentially inputs from previous task executions
        #  native constants are just bound to this specific task (default values for a task input)
        #  Also along with promises and constants, there could be dictionary or list of promises or constants
        try:
            literals = translate_inputs_to_literals(
                ctx,
                incoming_values=kwargs,
                flyte_interface_types=self.interface.inputs,
                native_types=self.get_input_types(),  # type: ignore
            )
        except TypeTransformerFailedError as exc:
            exc.args = (f"Failed to convert inputs of task '{self.name}':\n  {exc.args[0]}",)
            raise
        input_literal_map = _literal_models.LiteralMap(literals=literals)

        # if metadata.cache is set, check memoized version
        local_config = LocalConfig.auto()
        if self.metadata.cache and local_config.cache_enabled:
            if local_config.cache_overwrite:
                outputs_literal_map = None
                logger.info("Cache overwrite, task will be executed now")
            else:
                logger.info(
                    f"Checking cache for task named {self.name}, cache version {self.metadata.cache_version} "
                    f", inputs: {kwargs}, and ignore input vars: {self.metadata.cache_ignore_input_vars}"
                )
                outputs_literal_map = LocalTaskCache.get(
                    self.name, self.metadata.cache_version, input_literal_map, self.metadata.cache_ignore_input_vars
                )
                # The cache returns None iff the key does not exist in the cache
                if outputs_literal_map is None:
                    logger.info("Cache miss, task will be executed now")
                else:
                    logger.info("Cache hit")
            if outputs_literal_map is None:
                outputs_literal_map = self.sandbox_execute(ctx, input_literal_map)
                # TODO: need `native_inputs`
                LocalTaskCache.set(
                    self.name,
                    self.metadata.cache_version,
                    input_literal_map,
                    self.metadata.cache_ignore_input_vars,
                    outputs_literal_map,
                )
                logger.info(
                    f"Cache set for task named {self.name}, cache version {self.metadata.cache_version} "
                    f", inputs: {kwargs}, and ignore input vars: {self.metadata.cache_ignore_input_vars}"
                )
        else:
            # This code should mirror the call to `sandbox_execute` in the above cache case.
            # Code is simpler with duplication and less metaprogramming, but introduces regressions
            # if one is changed and not the other.
            outputs_literal_map = self.sandbox_execute(ctx, input_literal_map)

        outputs_literals = outputs_literal_map.literals

        # TODO maybe this is the part that should be done for local execution, we pass the outputs to some special
        #    location, otherwise we dont really need to right? The higher level execute could just handle literalMap
        # After running, we again have to wrap the outputs, if any, back into Promise objects
        output_names = list(self.interface.outputs.keys())  # type: ignore
        if len(output_names) != len(outputs_literals):
            # Length check, clean up exception
            raise AssertionError(f"Length difference {len(output_names)} {len(outputs_literals)}")

        # Tasks that don't return anything still return a VoidPromise
        if len(output_names) == 0:
            return VoidPromise(self.name)

        vals = [Promise(var, outputs_literals[var]) for var in output_names]
        return create_task_output(vals, self.python_interface)

    def __call__(self, *args: object, **kwargs: object) -> Union[Tuple[Promise], Promise, VoidPromise, Tuple, None]:
        return flyte_entity_call_handler(self, *args, **kwargs)  # type: ignore

    def compile(self, ctx: FlyteContext, *args, **kwargs):
        raise NotImplementedError

    def get_container(self, settings: SerializationSettings) -> Optional[_task_model.Container]:
        """
        Returns the container definition (if any) that is used to run the task on hosted Flyte.
        """
        return None

    def get_k8s_pod(self, settings: SerializationSettings) -> Optional[_task_model.K8sPod]:
        """
        Returns the kubernetes pod definition (if any) that is used to run the task on hosted Flyte.
        """
        return None

    def get_sql(self, settings: SerializationSettings) -> Optional[_task_model.Sql]:
        """
        Returns the Sql definition (if any) that is used to run the task on hosted Flyte.
        """
        return None

    def get_custom(self, settings: SerializationSettings) -> Optional[Dict[str, Any]]:
        """
        Return additional plugin-specific custom data (if any) as a serializable dictionary.
        """
        return None

    def get_config(self, settings: SerializationSettings) -> Optional[Dict[str, str]]:
        """
        Returns the task config as a serializable dictionary. This task config consists of metadata about the custom
        defined for this task.
        """
        return None

    def get_extended_resources(self, settings: SerializationSettings) -> Optional[tasks_pb2.ExtendedResources]:
        """
        Returns the extended resources to allocate to the task on hosted Flyte.
        """
        return None

    def local_execution_mode(self) -> ExecutionState.Mode:
        """ """
        return ExecutionState.Mode.LOCAL_TASK_EXECUTION

    def sandbox_execute(
        self,
        ctx: FlyteContext,
        input_literal_map: _literal_models.LiteralMap,
    ) -> _literal_models.LiteralMap:
        """
        Call dispatch_execute, in the context of a local sandbox execution. Not invoked during runtime.
        """
        es = cast(ExecutionState, ctx.execution_state)
        b = cast(ExecutionParameters, es.user_space_params).with_task_sandbox()
        ctx = ctx.current_context().with_execution_state(es.with_params(user_space_params=b.build())).build()
        return self.dispatch_execute(ctx, input_literal_map)

    @abstractmethod
    def dispatch_execute(
        self,
        ctx: FlyteContext,
        input_literal_map: _literal_models.LiteralMap,
    ) -> _literal_models.LiteralMap:
        """
        This method translates Flyte's Type system based input values and invokes the actual call to the executor
        This method is also invoked during runtime.
        """
        pass

    @abstractmethod
    def pre_execute(self, user_params: ExecutionParameters) -> ExecutionParameters:
        """
        This is the method that will be invoked directly before executing the task method and before all the inputs
        are converted. One particular case where this is useful is if the context is to be modified for the user process
        to get some user space parameters. This also ensures that things like SparkSession are already correctly
        setup before the type transformers are called

        This should return either the same context of the mutated context
        """
        pass

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        """
        This method will be invoked to execute the task.
        """
        pass


T = TypeVar("T")


class PythonTask(TrackedInstance, Task, Generic[T]):
    """
    Base Class for all Tasks with a Python native ``Interface``. This should be directly used for task types, that do
    not have a python function to be executed. Otherwise refer to {{< py_class_ref flytekit.PythonFunctionTask >}}.
    """

    def __init__(
        self,
        task_type: str,
        name: str,
        task_config: Optional[T] = None,
        interface: Optional[Interface] = None,
        environment: Optional[Dict[str, str]] = None,
        disable_deck: Optional[bool] = None,
        enable_deck: Optional[bool] = None,
        deck_fields: Optional[Tuple[DeckField, ...]] = (
            DeckField.SOURCE_CODE,
            DeckField.DEPENDENCIES,
            DeckField.TIMELINE,
            DeckField.INPUT,
            DeckField.OUTPUT,
        ),
        **kwargs,
    ):
        """
        Args:
            task_type (str): defines a unique task-type for every new extension. If a backend plugin is required then
                this has to be done in-concert with the backend plugin identifier
            name (str): A unique name for the task instantiation. This is unique for every instance of task.
            task_config (T): Configuration for the task. This is used to configure the specific plugin that handles this
                task
            interface (Optional[Interface]): A python native typed interface ``(inputs) -> outputs`` that declares the
                signature of the task
            environment (Optional[Dict[str, str]]): Any environment variables that should be supplied during the
                execution of the task. Supplied as a dictionary of key/value pairs
            disable_deck (bool): (deprecated) If true, this task will not output deck html file
            enable_deck (bool): If true, this task will output deck html file
            deck_fields (Tuple[DeckField]): Tuple of decks to be
                generated for this task. Valid values can be selected from fields of ``flytekit.deck.DeckField`` enum
        """
        super().__init__(
            task_type=task_type,
            name=name,
            interface=transform_interface_to_typed_interface(interface, allow_partial_artifact_id_binding=True),
            **kwargs,
        )
        self._python_interface = interface if interface else Interface()
        self._environment = environment if environment else {}
        self._task_config = task_config

        # first we resolve the conflict between params regarding decks, if any two of [disable_deck, enable_deck]
        # are set, we raise an error
        configured_deck_params = [disable_deck is not None, enable_deck is not None]
        if sum(configured_deck_params) > 1:
            raise ValueError("only one of [disable_deck, enable_deck] can be set")

        if disable_deck is not None:
            warnings.warn(
                "disable_deck was deprecated in 1.10.0, please use enable_deck instead",
                FutureWarning,
            )

        if enable_deck is not None:
            self._disable_deck = not enable_deck
        elif disable_deck is not None:
            self._disable_deck = disable_deck
        else:
            self._disable_deck = True

        self._deck_fields = list(deck_fields) if (deck_fields is not None and self.enable_deck) else []

        deck_members = set([_field for _field in DeckField])
        # enumerate additional decks, check if any of them are invalid
        for deck in self._deck_fields:
            if deck not in deck_members:
                raise ValueError(
                    f"Element {deck} from deck_fields param is not a valid deck field. Please use one of {deck_members}"
                )

        if self._python_interface.docstring:
            if self.docs is None:
                self._docs = Documentation(
                    short_description=self._python_interface.docstring.short_description,
                    long_description=Description(value=self._python_interface.docstring.long_description),
                )
            else:
                if self._python_interface.docstring.short_description:
                    cast(
                        Documentation, self._docs
                    ).short_description = self._python_interface.docstring.short_description
                if self._python_interface.docstring.long_description:
                    cast(Documentation, self._docs).long_description = Description(
                        value=self._python_interface.docstring.long_description
                    )

    # TODO lets call this interface and the other as flyte_interface?
    @property
    def python_interface(self) -> Interface:
        """
        Returns this task's python interface.
        """
        return self._python_interface

    @property
    def task_config(self) -> Optional[T]:
        """
        Returns the user-specified task config which is used for plugin-specific handling of the task.
        """
        return self._task_config

    def get_type_for_input_var(self, k: str, v: Any) -> Type[Any]:
        """
        Returns the python type for an input variable by name.
        """
        return self._python_interface.inputs[k]

    def get_type_for_output_var(self, k: str, v: Any) -> Type[Any]:
        """
        Returns the python type for the specified output variable by name.
        """
        return self._python_interface.outputs[k]

    def get_input_types(self) -> Dict[str, type]:
        """
        Returns the names and python types as a dictionary for the inputs of this task.
        """
        return self._python_interface.inputs

    def construct_node_metadata(self) -> _workflow_model.NodeMetadata:
        """
        Used when constructing the node that encapsulates this task as part of a broader workflow definition.
        """
        return _workflow_model.NodeMetadata(
            name=extract_obj_name(self.name),
            timeout=self.metadata.timeout,
            retries=self.metadata.retry_strategy,
            interruptible=self.metadata.interruptible,
        )

    def compile(self, ctx: FlyteContext, *args, **kwargs) -> Optional[Union[Tuple[Promise], Promise, VoidPromise]]:
        """
        Generates a node that encapsulates this task in a workflow definition.
        """
        return create_and_link_node(ctx, entity=self, **kwargs)

    @property
    def _outputs_interface(self) -> Dict[Any, Variable]:
        return self.interface.outputs  # type: ignore

    def _literal_map_to_python_input(
        self, literal_map: _literal_models.LiteralMap, ctx: FlyteContext
    ) -> Dict[str, Any]:
        return TypeEngine.literal_map_to_kwargs(ctx, literal_map, self.python_interface.inputs)

    async def _output_to_literal_map(self, native_outputs: Dict[int, Any], ctx: FlyteContext):
        expected_output_names = list(self._outputs_interface.keys())
        if len(expected_output_names) == 1:
            # Here we have to handle the fact that the task could've been declared with a typing.NamedTuple of
            # length one. That convention is used for naming outputs - and single-length-NamedTuples are
            # particularly troublesome, but elegant handling of them is not a high priority
            # Again, we're using the output_tuple_name as a proxy.
            if self.python_interface.output_tuple_name and isinstance(native_outputs, tuple):
                native_outputs_as_map = {expected_output_names[0]: native_outputs[0]}
            else:
                native_outputs_as_map = {expected_output_names[0]: native_outputs}
        elif len(expected_output_names) == 0:
            native_outputs_as_map = {}
        else:
            native_outputs_as_map = {expected_output_names[i]: native_outputs[i] for i, _ in enumerate(native_outputs)}

        # We manually construct a LiteralMap here because task inputs and outputs actually violate the assumption
        # built into the IDL that all the values of a literal map are of the same type.
        with timeit("Translate the output to literals"):
            literals = {}
            omt = ctx.output_metadata_tracker
            # Here is where we iterate through the outputs, need to call new type engine.
            for i, (k, v) in enumerate(native_outputs_as_map.items()):
                literal_type = self._outputs_interface[k].type
                py_type = self.get_type_for_output_var(k, v)

                if isinstance(v, tuple):
                    raise TypeError(f"Output({k}) in task '{self.name}' received a tuple {v}, instead of {py_type}")
                literals[k] = asyncio.create_task(TypeEngine.async_to_literal(ctx, v, py_type, literal_type))

            await asyncio.gather(*literals.values(), return_exceptions=True)

            for i, (k2, v2) in enumerate(literals.items()):
                if v2.exception() is not None:
                    # only show the name of output key if it's user-defined (by default Flyte names these as "o<n>")
                    key = k2 if k2 != f"o{i}" else i
                    e: BaseException = v2.exception()  # type: ignore  # we know this is not optional
                    py_type = self.get_type_for_output_var(k2, native_outputs_as_map[k2])
                    e.args = (
                        f"Failed to convert outputs of task '{self.name}' at position {key}.\n"
                        f"Failed to convert type {type(native_outputs_as_map[expected_output_names[i]])} to type {py_type}.\n"
                        f"Error Message: {e.args[0]}.",
                    )
                    raise e
                literals[k2] = v2.result()

            if omt is not None:
                for i, (k, v) in enumerate(native_outputs_as_map.items()):
                    # Now check if there is any output metadata associated with this output variable and attach it to the
                    # literal
                    om = omt.get(v)
                    if om:
                        metadata = {}
                        if om.additional_items:
                            for ii in om.additional_items:
                                md_key, md_val = ii.serialize_to_string(ctx, k)
                                metadata[md_key] = md_val
                            logger.info(f"Adding {om.additional_items} additional metadata items {metadata} for {k}")
                        if om.dynamic_partitions or om.time_partition:
                            a = art_id.ArtifactID(
                                partitions=idl_partitions_from_dict(om.dynamic_partitions),
                                time_partition=idl_time_partition_from_datetime(
                                    om.time_partition, om.artifact.time_partition_granularity
                                ),
                            )
                            s = a.SerializeToString()
                            encoded = b64encode(s).decode("utf-8")
                            metadata[DYNAMIC_PARTITIONS] = encoded
                        if metadata:
                            literals[k].set_metadata(metadata)  # type: ignore  # we know these have been resolved

        return _literal_models.LiteralMap(literals=literals), native_outputs_as_map

    def _write_decks(self, native_inputs, native_outputs_as_map, ctx, new_user_params):
        if self._disable_deck is False:
            from flytekit.deck.deck import Deck, DeckField, _output_deck

            INPUT = DeckField.INPUT
            OUTPUT = DeckField.OUTPUT

            if DeckField.INPUT in self.deck_fields:
                input_deck = Deck(INPUT.value)
                for k, v in native_inputs.items():
                    input_deck.append(TypeEngine.to_html(ctx, v, self.get_type_for_input_var(k, v)))

            if DeckField.OUTPUT in self.deck_fields:
                output_deck = Deck(OUTPUT.value)
                for k, v in native_outputs_as_map.items():
                    output_deck.append(TypeEngine.to_html(ctx, v, self.get_type_for_output_var(k, v)))

            if ctx.execution_state and ctx.execution_state.is_local_execution():
                # When we run the workflow remotely, flytekit outputs decks at the end of _dispatch_execute
                _output_deck(self.name.split(".")[-1], new_user_params)

    async def _async_execute(self, native_inputs, native_outputs, ctx, exec_ctx, new_user_params):
        native_outputs = await native_outputs
        native_outputs = self.post_execute(new_user_params, native_outputs)
        literals_map, native_outputs_as_map = await self._output_to_literal_map(native_outputs, exec_ctx)
        self._write_decks(native_inputs, native_outputs_as_map, ctx, new_user_params)
        return literals_map

    def dispatch_execute(
        self, ctx: FlyteContext, input_literal_map: _literal_models.LiteralMap
    ) -> Union[_literal_models.LiteralMap, _dynamic_job.DynamicJobSpec, Coroutine]:
        """
        This method translates Flyte's Type system based input values and invokes the actual call to the executor
        This method is also invoked during runtime.

        * ``VoidPromise`` is returned in the case when the task itself declares no outputs.
        * ``Literal Map`` is returned when the task returns either one more outputs in the declaration. Individual outputs
          may be none
        * ``DynamicJobSpec`` is returned when a dynamic workflow is executed
        """

        # Invoked before the task is executed
        new_user_params = self.pre_execute(ctx.user_space_params)

        if self.enable_deck and ctx.user_space_params is not None:
            if DeckField.TIMELINE.value in self.deck_fields:
                ctx.user_space_params.decks.append(ctx.user_space_params.timeline_deck)
            new_user_params = ctx.user_space_params.with_enable_deck(enable_deck=True).build()

        # Create another execution context with the new user params, but let's keep the same working dir
        with FlyteContextManager.with_context(
            ctx.with_execution_state(
                cast(ExecutionState, ctx.execution_state).with_params(user_space_params=new_user_params)
            )
            # type: ignore
        ) as exec_ctx:
            is_local_execution = cast(ExecutionState, exec_ctx.execution_state).is_local_execution()
            # TODO We could support default values here too - but not part of the plan right now
            # Translate the input literals to Python native
            try:
                native_inputs = self._literal_map_to_python_input(input_literal_map, exec_ctx)
            except (FlyteUploadDataException, FlyteDownloadDataException):
                raise
            except Exception as e:
                if is_local_execution:
                    e.args = (f"Error encountered while converting inputs of '{self.name}':\n  {e}",)
                    raise
                raise FlyteNonRecoverableSystemException(e) from e
            # TODO: Logger should auto inject the current context information to indicate if the task is running within
            #   a workflow or a subworkflow etc
            logger.info(f"Invoking {self.name} with inputs: {native_inputs}")
            with timeit("Execute user level code"):
                try:
                    native_outputs = self.execute(**native_inputs)
                except Exception as e:
                    if is_local_execution:
                        # If the task is being executed locally, we want to raise the original exception
                        e.args = (f"Error encountered while executing '{self.name}':\n  {e}",)
                        raise
                    raise FlyteUserRuntimeException(e) from e

            # Lets run the post_execute method. This may result in a IgnoreOutputs Exception, which is
            # bubbled up to be handled at the callee layer.
            native_outputs = self.post_execute(new_user_params, native_outputs)

            # Short circuit the translation to literal map because what's returned may be a dj spec (or an
            # already-constructed LiteralMap if the dynamic task was a no-op), not python native values
            # dynamic_execute returns a literal map in local execute so this also gets triggered.
            if isinstance(
                native_outputs,
                (_literal_models.LiteralMap, _dynamic_job.DynamicJobSpec),
            ):
                return native_outputs

            try:
                with timeit("dispatch execute"):
                    literals_map, native_outputs_as_map = run_sync(
                        self._output_to_literal_map, native_outputs, exec_ctx
                    )
                self._write_decks(native_inputs, native_outputs_as_map, ctx, new_user_params)
            except (FlyteUploadDataException, FlyteDownloadDataException):
                raise
            except Exception as e:
                if is_local_execution:
                    raise
                raise FlyteNonRecoverableSystemException(e) from e

            # After the execution has been successfully completed
            return literals_map

    def pre_execute(self, user_params: Optional[ExecutionParameters]) -> Optional[ExecutionParameters]:  # type: ignore
        """
        This is the method that will be invoked directly before executing the task method and before all the inputs
        are converted. One particular case where this is useful is if the context is to be modified for the user process
        to get some user space parameters. This also ensures that things like SparkSession are already correctly
        setup before the type transformers are called

        This should return either the same context of the mutated context
        """
        return user_params

    @abstractmethod
    def execute(self, **kwargs) -> Any:
        """
        This method will be invoked to execute the task.
        """
        pass

    def post_execute(self, user_params: Optional[ExecutionParameters], rval: Any) -> Any:
        """
        Post execute is called after the execution has completed, with the user_params and can be used to clean-up,
        or alter the outputs to match the intended tasks outputs. If not overridden, then this function is a No-op

        Args:
            rval is returned value from call to execute
            user_params: are the modified user params as created during the pre_execute step
        """
        return rval

    @property
    def environment(self) -> Dict[str, str]:
        """
        Any environment variables that supplied during the execution of the task.
        """
        return self._environment

    @property
    def disable_deck(self) -> bool:
        """
        If true, this task will not output deck html file
        """
        warnings.warn(
            "`disable_deck` is deprecated and will be removed in the future.\n" "Please use `enable_deck` instead.",
            DeprecationWarning,
        )
        return self._disable_deck

    @property
    def enable_deck(self) -> bool:
        """
        If true, this task will output deck html file
        """
        return not self._disable_deck

    @property
    def deck_fields(self) -> List[DeckField]:
        """
        If not empty, this task will output deck html file for the specified decks
        """
        return self._deck_fields


class TaskResolverMixin(object):
    """
    Flytekit tasks interact with the Flyte platform very, very broadly in two steps. They need to be uploaded to Admin,
    and then they are run by the user upon request (either as a single task execution or as part of a workflow). In any
    case, at execution time, for most tasks (that is those that generate a container target) the container image
    containing the task needs to be spun up again at which point the container needs to know which task it's supposed
    to run and how to rehydrate the task object.

    For example, the serialization of a simple task ::

        # in repo_root/workflows/example.py
        @task
        def t1(...) -> ...: ...

    might result in a container with arguments like ::

        pyflyte-execute --inputs s3://path/inputs.pb --output-prefix s3://outputs/location \
        --raw-output-data-prefix /tmp/data \
        --resolver flytekit.core.python_auto_container.default_task_resolver \
        -- \
        task-module repo_root.workflows.example task-name t1

    At serialization time, the container created for the task will start out automatically with the ``pyflyte-execute``
    bit, along with the requisite input/output args and the offloaded data prefix. Appended to that will be two things,

    #. the ``location`` of the task's task resolver, followed by two dashes, followed by
    #. the arguments provided by calling the ``loader_args`` function below.

    The ``default_task_resolver`` declared below knows that

    * When ``loader_args`` is called on a task, to look up the module the task is in, and the name of the task (the
      key of the task in the module, either the function name, or the variable it was assigned to).
    * When ``load_task`` is called, it interprets the first part of the command as the module to call
      ``importlib.import_module`` on, and then looks for a key ``t1``.

    This is just the default behavior. Users should feel free to implement their own resolvers.
    """

    @property
    @abstractmethod
    def location(self) -> str:
        pass

    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def load_task(self, loader_args: List[str]) -> Task:
        """
        Given the set of identifier keys, should return one Python Task or raise an error if not found
        """
        pass

    @abstractmethod
    def loader_args(self, settings: SerializationSettings, t: Task) -> List[str]:
        """
        Return a list of strings that can help identify the parameter Task
        """
        pass

    @abstractmethod
    def get_all_tasks(self) -> List[Task]:
        """
        Future proof method. Just making it easy to access all tasks (Not required today as we auto register them)
        """
        pass

    def task_name(self, t: Task) -> Optional[str]:
        """
        Overridable function that can optionally return a custom name for a given task
        """
        return None
