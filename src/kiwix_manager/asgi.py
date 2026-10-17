from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import MutableMapping
from typing import Any

# ASGI messages are heterogeneous dicts by specification; there is no narrower honest type.
Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
