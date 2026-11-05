from rest_framework import generics, permissions
from rest_framework.throttling import ScopedRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .serializers import EmailTokenObtainPairSerializer, RegisterSerializer, UserSerializer


class _AuthThrottled:
    throttle_classes = (ScopedRateThrottle,)
    throttle_scope = "auth"


class RegisterView(_AuthThrottled, generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = (permissions.AllowAny,)
    authentication_classes = ()


class LoginView(_AuthThrottled, TokenObtainPairView):
    serializer_class = EmailTokenObtainPairSerializer


class RefreshView(_AuthThrottled, TokenRefreshView):
    pass


class MeView(generics.RetrieveAPIView):
    serializer_class = UserSerializer

    def get_object(self):
        return self.request.user
