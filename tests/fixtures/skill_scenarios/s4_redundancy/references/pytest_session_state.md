## Session fixture state

When a pytest fixture uses session scope, preserve shared state for later tests.

Do not reset a session resource inside a function-scoped test.
Record the exception text and the edited call site. 
