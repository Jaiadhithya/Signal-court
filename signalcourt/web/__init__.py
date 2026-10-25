"""Local operator console; pipeline imports are deferred to the startup worker."""


def create_app(*, home=None):
    from .app import create_app as factory

    return factory(home=home)
