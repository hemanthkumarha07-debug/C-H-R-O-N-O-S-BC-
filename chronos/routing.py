from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    # This matches the URL we put in the HTML template: /ws/chronos/
    re_path(r'ws/chronos/$', consumers.ChronosConsumer.as_asgi()),
]