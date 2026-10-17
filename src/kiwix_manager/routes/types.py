from litestar import Request
from litestar.connection import ASGIConnection
from litestar.datastructures import State

ManagerRequest = Request[None, None, State]
ManagerConnection = ASGIConnection[object, None, None, State]
