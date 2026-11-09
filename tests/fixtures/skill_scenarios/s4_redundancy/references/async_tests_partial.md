## Coroutine tests

When pytest runs a coroutine test, await the coroutine before asserting the result.

Await the coroutine and then assert the returned value.
Record the exception text and the edited call site. 
Close the event loop after the assertion.
