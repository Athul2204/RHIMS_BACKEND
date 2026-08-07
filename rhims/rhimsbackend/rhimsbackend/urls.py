from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/auth/", include("authentication.urls")),
    path("api/administration/", include("administration.urls")),
    path("api/reception/", include("reception.urls")),
    path("api/doctor/", include("doctor.urls")),
    path("api/pharmacist/", include("pharmacist.urls")),
    path("api/lab/", include("lab.urls")),
    path("api/manager/", include("manager.urls")),
    path("api/public/", include("manager.public_urls")),
]

# Serve uploaded media (doctor photos, testimonial photos, etc.) locally
# in DEBUG. In production (PythonAnywhere), the web app's Static/Media
# file mapping serves MEDIA_URL -> MEDIA_ROOT directly instead — this
# block is a no-op there since DEBUG=False.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)