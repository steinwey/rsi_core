## Function fixture scope

When a pytest fixture uses function scope, isolate mutable state from later tests.

Create the resource inside the fixture and close it in teardown.
Record the exception text and the edited call site. 
