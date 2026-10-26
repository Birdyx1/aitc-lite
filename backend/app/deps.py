from fastapi import Request


def get_session(request: Request):
    with request.app.state.session_factory() as session:
        yield session
