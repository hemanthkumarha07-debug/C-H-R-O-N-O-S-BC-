from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('api/state/', views.api_state, name='api_state'),
    path('api/control/', views.api_control, name='api_control'),
    path('api/update/', views.engine_update, name='engine_update'),
    path('api/export/strikes.csv', views.export_strikes_csv, name='export_strikes_csv'),
]
