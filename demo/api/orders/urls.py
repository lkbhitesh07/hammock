from django.urls import path

from . import views

urlpatterns = [
    path("health", views.health),
    path("api/products", views.products),
    path("api/orders", views.orders),
]
