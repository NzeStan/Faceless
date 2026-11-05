from django.contrib import admin

from .models import MediaAsset, RenderJob, Voiceover

for model in (MediaAsset, Voiceover, RenderJob):
    admin.site.register(model)
