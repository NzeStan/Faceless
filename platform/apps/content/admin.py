from django.contrib import admin

from .models import ContentEvent, ContentItem, Niche, Script

for model in (Niche, ContentItem, ContentEvent, Script):
    admin.site.register(model)
