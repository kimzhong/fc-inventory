# Test fixtures for fc-inventory v3.0.0.
#
# Provides:
#   - httpx MockTransport factory for testing the FC client + collector
#     against canned JSON responses (the same shape the real FusionCompute
#     VRM returns).
#   - A TestClient fixture with the app's lifespan started and a stub
#     httpx pool injected so the routes can run end-to-end.
