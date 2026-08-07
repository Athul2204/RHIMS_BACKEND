# manager/public_urls.py
#
# Routes for the unauthenticated public marketing site. Deliberately kept
# in a *separate urls module* from manager/urls.py (which is mounted at
# /api/manager/ and requires auth) even though both live inside the same
# `manager` Django app — this file is mounted at /api/public/ directly
# from rhimsbackend/urls.py. No new Django app was created; this is just
# a second URLConf module inside the existing app.
from django.urls import path
from .views import (
    PublicDoctorListView,
    PublicDoctorDetailView,
    PublicAvailabilityView,
    PublicTestimonialListView,
    PublicYoutubeVideoListView,
    PublicInstagramPostListView,
    PublicFacebookPostListView,
    PublicMediaEventListView,
    PublicMediaEventDetailView,
    PublicGalleryImageListView,
    PublicSpecialtyListView,
    PublicSpecialtyDetailView,
    PublicTreatmentDetailView,
    PublicBlogListView,
    PublicBlogDetailView,
    PublicPreBookingCreateView,
    PublicMrdCheckView,
    PublicContactInquiryCreateView,
    PublicBranchListView,
    PublicBranchChoicesListView,
)

urlpatterns = [
    path("doctors/",         PublicDoctorListView.as_view(),          name="public-doctor-list"),
    path("doctors/<int:doctor_id>/", PublicDoctorDetailView.as_view(), name="public-doctor-detail"),
    path("availability/",    PublicAvailabilityView.as_view(),        name="public-availability"),
    path("testimonials/",    PublicTestimonialListView.as_view(),     name="public-testimonial-list"),
    path("youtube-videos/",  PublicYoutubeVideoListView.as_view(),    name="public-youtube-list"),
    path("instagram-posts/", PublicInstagramPostListView.as_view(),   name="public-instagram-list"),
    path("facebook-posts/",  PublicFacebookPostListView.as_view(),    name="public-facebook-list"),
    path("media-events/",             PublicMediaEventListView.as_view(),   name="public-media-event-list"),
    path("media-events/<slug:slug>/", PublicMediaEventDetailView.as_view(), name="public-media-event-detail"),
    path("gallery/",         PublicGalleryImageListView.as_view(),    name="public-gallery-list"),
    path("branches/",        PublicBranchListView.as_view(),          name="public-branch-list"),
    path("branches/choices/", PublicBranchChoicesListView.as_view(),  name="public-branch-choices"),
    path("specialities/",              PublicSpecialtyListView.as_view(),   name="public-specialty-list"),
    path("specialities/<slug:slug>/",  PublicSpecialtyDetailView.as_view(), name="public-specialty-detail"),
    path("procedures/<slug:slug>/",    PublicTreatmentDetailView.as_view(), name="public-procedure-detail"),
    path("blogs/",                     PublicBlogListView.as_view(),        name="public-blog-list"),
    path("blogs/<slug:slug>/",         PublicBlogDetailView.as_view(),      name="public-blog-detail"),
    path("prebook/",         PublicPreBookingCreateView.as_view(),    name="public-prebook"),
    # ✅ FIX: PublicMrdCheckView existed in views.py (its own docstring
    # documents this exact route) but was never wired here — the
    # frontend's live MRD/name/phone/gender pre-check 404'd.
    path("mrd-check/",       PublicMrdCheckView.as_view(),            name="public-mrd-check"),
    path("contact/",         PublicContactInquiryCreateView.as_view(), name="public-contact"),
]