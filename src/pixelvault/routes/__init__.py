from . import auth, albums, share, media, api, admin


def register_all(app):
    auth.register(app)
    albums.register(app)
    share.register(app)
    media.register(app)
    api.register(app)
    admin.register(app)
