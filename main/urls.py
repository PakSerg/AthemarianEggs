from django.conf import settings
from django.urls import path
from . import views

app_name = 'main'

urlpatterns = [
    path('', views.HomeView.as_view(), name='home'),
    path('addresses/', views.AddressesView.as_view(), name='addresses'),
    path('products/', views.ProductsListView.as_view(), name='products'),
    path('plug-preview/', views.PlugView.as_view(), name='plug'),
]

if settings.DEBUG:
    urlpatterns += [
        path('404-preview/', views.NotFoundPreviewView.as_view(), name='not_found_preview'),
    ]