import factory
from django.contrib.auth import get_user_model
from social_publisher.adapters.youtube import REQUIRED_SCOPES
from social_publisher.models import ConnectedAccount, OAuthToken
from social_publisher.services import _owner_fields
from social_publisher.types import TokenBundle

from apps.content.models import ContentItem, Niche

ACCESS_TOKEN = "ya29.PLATFORM-ACCESS-SECRET"
REFRESH_TOKEN = "1//PLATFORM-REFRESH-SECRET"


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = get_user_model()
        skip_postgeneration_save = True

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    password = factory.PostGenerationMethodCall("set_password", "a-Strong-pass-123")


class NicheFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Niche

    owner = factory.SubFactory(UserFactory)
    name = "Space facts"
    audience = "Curious adults"
    tone = "Friendly"
    keywords = ["space"]
    is_factual = True


class ContentItemFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = ContentItem

    owner = factory.SubFactory(UserFactory)
    niche = factory.LazyAttribute(lambda item: NicheFactory(owner=item.owner))
    topic = factory.Sequence(lambda n: f"Topic number {n}")


def connect_account(owner) -> ConnectedAccount:
    account = ConnectedAccount.objects.create(
        platform="youtube",
        external_account_id=f"UC{owner.pk.hex[:10]}",
        display_name="Channel",
        scopes=list(REQUIRED_SCOPES),
        **_owner_fields(owner),
    )
    record = OAuthToken(account=account)
    record.set_bundle(TokenBundle(ACCESS_TOKEN, REFRESH_TOKEN))
    record.save()
    return account
