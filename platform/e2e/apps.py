from django.apps import AppConfig


class E2EConfig(AppConfig):
    name = "e2e"

    def ready(self):
        # Done here because importing the adapter from settings would load models too early.
        import social_publisher.adapters.youtube as youtube

        base = "http://fake-google:9999"
        youtube.TOKEN_URL = f"{base}/token"
        youtube.REVOKE_URL = f"{base}/revoke"
        youtube.API_URL = f"{base}/youtube/v3"
        youtube.UPLOAD_URL = f"{base}/upload"
