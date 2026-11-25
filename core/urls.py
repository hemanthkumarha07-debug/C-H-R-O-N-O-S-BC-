from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', include('chronos.urls')), # This directs the root URL to your dashboard
]