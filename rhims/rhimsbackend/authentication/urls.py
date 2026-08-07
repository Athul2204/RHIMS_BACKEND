from django.urls import path
from .views import LoginView, CookieTokenRefreshView, MeView, LogoutView, LoginActivityView

urlpatterns = [
    path("login/",         LoginView.as_view(),              name="login"),
    path("refresh/",       CookieTokenRefreshView.as_view(), name="token_refresh"),
    path("me/",            MeView.as_view(),                 name="me"),
    path("logout/",        LogoutView.as_view(),             name="logout"),
    path("login-history/", LoginActivityView.as_view(),      name="login_history"),
]