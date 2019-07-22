import asyncio
from weakref import ref

from typing import (
    List,
    Dict,
    Tuple,
    Mapping,
    Union,
    Any,
    Set,
    Generic,
    TypeVar,
    Optional,
    AsyncIterator,
    Awaitable,
)

from .element import AbstractElement
from .events import EventHandler

try:
    import vdom
except ImportError:
    vdom = None


RenderBundle = Tuple[
    str,  # element ID for the update's source
    Dict[str, Dict[str, Any]],  # maps element IDs to new models
    List[str],  # list element IDs that have been deleted
]


class RenderError(Exception):
    """An error occured while rendering element models."""


class Layout:
    """Renders the models generated by :class:`AbstractElement` objects."""

    __slots__ = (
        "_loop",
        "_rendering_queue",
        "_root",
        "_event_handlers",
        "_element_state",
        "__weakref__",
    )

    def __init__(
        self, root: "AbstractElement", loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        if loop is None:
            loop = asyncio.get_event_loop()
        if not isinstance(root, AbstractElement):
            raise TypeError("Expected an AbstractElement, not %r" % root)
        self._loop = loop
        self._element_state: Dict[str, Dict[str, Any]] = {}
        self._event_handlers: Dict[str, EventHandler] = {}
        self._root = root
        self._rendering_queue: FutureQueue[RenderBundle] = FutureQueue()
        self.update(root)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        return self._loop

    @property
    def root(self) -> str:
        return self._root.id

    async def trigger(self, target: str, data: List[Any]) -> None:
        """Trigger an event handler

        Parameters:
            target: The ID of the event handler
            data: Event data passed to the event handler.
        """
        # It is possible for an element in the frontend to produce an event
        # associated with a backend model that has been deleted. We only handle
        # events if the element and the handler exist in the backend. Otherwise
        # we just ignore the event.
        if target in self._event_handlers:
            await self._event_handlers[target](data)

    def update(self, element: "AbstractElement") -> None:
        self._rendering_queue.put(self._render(element))

    async def render(self) -> RenderBundle:
        return await self._rendering_queue.get()

    async def _render(self, element: AbstractElement) -> RenderBundle:
        # current element ids
        current: Set[str] = set(self._element_state)

        # all element updates
        new: Dict[str, Dict[str, Any]] = {}

        parent = self._element_parent(element)
        async for element_id, model in self._render_element(element, parent):
            new[element_id] = model

        # all deleted element ids
        old: List[str] = list(current.difference(self._element_state))
        return element.id, new, old

    async def _render_element(
        self, element: "AbstractElement", parent_element_id: Optional[str]
    ) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
        try:
            element_id = element.id
            if self._has_element_state(element_id):
                await self._reset_element_state(element)
            else:
                await self._create_element_state(element, parent_element_id)

            model = await element.render()

            if isinstance(model, AbstractElement):
                model = {"tagName": "div", "children": [model]}

            async for i, m in self._render_model(model, element_id):
                yield i, m
        except Exception as error:
            raise RenderError(f"Failed to render {element}") from error

    async def _render_model(
        self, model: Mapping[str, Any], element_id: str
    ) -> AsyncIterator[Tuple[str, Dict[str, Any]]]:
        index = 0
        to_visit: List[Union[Mapping[str, Any], AbstractElement]] = [model]
        while index < len(to_visit):
            node = to_visit[index]
            if isinstance(node, AbstractElement):
                async for i, m in self._render_element(node, element_id):
                    yield i, m
            elif isinstance(node, Mapping):
                if "children" in node:
                    value = node["children"]
                    if isinstance(value, (list, tuple)):
                        to_visit.extend(value)
                    elif isinstance(value, (Mapping, AbstractElement)):
                        to_visit.append(value)
            elif vdom is not None and isinstance(node, vdom.VDOM):
                to_visit.append(_from_vdom(node))
            index += 1
        yield element_id, self._load_model(model, element_id)

    def _load_model(self, model: Mapping[str, Any], element_id: str) -> Dict[str, Any]:
        model = dict(model)
        if "children" in model:
            model["children"] = self._load_model_children(model["children"], element_id)
        handlers = self._load_event_handlers(model, element_id)
        if handlers:
            model["eventHandlers"] = handlers
        return model

    def _load_model_children(
        self, children: Union[List[Any], Tuple[Any, ...]], element_id: str
    ) -> List[Dict[str, Any]]:
        if not isinstance(children, (list, tuple)):
            children = [children]
        loaded_children = []
        for child in children:
            if isinstance(child, Mapping):
                child = {"type": "obj", "data": self._load_model(child, element_id)}
            elif isinstance(child, AbstractElement):
                child = {"type": "ref", "data": child.id}
            else:
                child = {"type": "str", "data": str(child)}
            loaded_children.append(child)
        return loaded_children

    def _load_event_handlers(
        self, model: Dict[str, Any], element_id: str
    ) -> Dict[str, Dict[str, Any]]:
        # gather event handler from eventHandlers and attributes fields
        handlers: Dict[str, EventHandler] = {}
        if "eventHandlers" in model:
            handlers.update(model["eventHandlers"])
        if "attributes" in model:
            attrs = model["attributes"]
            for k, v in list(attrs.items()):
                if callable(v):
                    if not isinstance(v, EventHandler):
                        h = handlers[k] = EventHandler()
                        h.add(attrs.pop(k))
                    else:
                        h = attrs.pop(k)
                        handlers[k] = h

        event_targets = {}
        for event, handler in handlers.items():
            handler_spec = handler.serialize()
            event_targets[event] = handler_spec
            self._event_handlers[handler.id] = handler
            self._element_state[element_id]["event_handlers"].append(handler.id)

        return event_targets

    def _has_element_state(self, element_id: str) -> bool:
        return element_id in self._element_state

    def _element_parent(self, element: AbstractElement) -> Optional[str]:
        try:
            parent_id: str = self._element_state[element.id]["parent"]
        except KeyError:
            if element.id != self.root:
                raise
            return None
        else:
            return parent_id

    async def _create_element_state(
        self, element: AbstractElement, parent_element_id: Optional[str]
    ) -> None:
        if parent_element_id is not None and self._has_element_state(parent_element_id):
            self._element_state[parent_element_id]["inner_elements"].add(element.id)
        self._element_state[element.id] = {
            "parent": parent_element_id,
            "inner_elements": set(),
            "event_handlers": [],
            "element_ref": ref(element),
        }
        await element.mount(self)

    async def _reset_element_state(self, element: AbstractElement) -> None:
        parent_element_id = self._element_state[element.id]["parent"]
        await self._delete_element_state(element.id, unmount=False)
        await self._create_element_state(element, parent_element_id)

    async def _delete_element_state(
        self, element_id: str, unmount: bool = True
    ) -> None:
        old = self._element_state.pop(element_id)
        parent_element_id = old["parent"]
        if self._has_element_state(parent_element_id):
            self._element_state[parent_element_id]["inner_elements"].remove(element_id)
        for handler_id in old["event_handlers"]:
            del self._event_handlers[handler_id]
        for i in old["inner_elements"]:
            # don't pass on 'unmount' since that only applies to the root
            await self._delete_element_state(i)
        element = old["element_ref"]()
        if element is not None and unmount:
            await element.unmount()


# future queue type
_FQT = TypeVar("_FQT")


class FutureQueue(Generic[_FQT]):
    """A queue which returns the result of futures as they complete."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue["asyncio.Future[_FQT]"] = asyncio.Queue()

    def put(self, awaitable: Awaitable[_FQT]) -> "asyncio.Future[_FQT]":
        """Put an awaitable in the queue

        The result will be returned by a call to :meth:`FutureQueue.get` only
        when the awaitable has completed.
        """

        async def wrapper() -> _FQT:
            try:
                return await awaitable
            finally:
                self._queue.put_nowait(future)

        future = asyncio.ensure_future(wrapper())
        return future

    async def get(self) -> _FQT:
        """Get the result of a queued awaitable that has completed."""
        future = await self._queue.get()
        return await future


def _from_vdom(node: Any) -> Dict[str, Any]:
    data = {
        "tagName": node.tag_name,
        "children": node.children,
        "attributes": node.attributes,
    }
    if node.style:
        data["attributes"]["style"] = node.style
    if node.event_handlers:
        data["eventHandlers"] = node.event_handlers
    if node.key:
        data["key"] = node.key
    return data
