import functools
import operator

import graphene
from django.db.models import Q
from graphene import relay
from graphene.relay.node import from_global_id
from graphene_django import DjangoObjectType
from graphene_django.filter import DjangoFilterConnectionField

from ...product import models
from ...product.templatetags.product_images import product_first_image
from ...product.utils import get_availability, products_visible_to_user
from ..core.types import Price, PriceRange
from ..utils import CategoryAncestorsCache
from .filters import ProductFilterSet
from .scalars import AttributesFilterScalar

CONTEXT_CACHE_NAME = '__cache__'
CACHE_ANCESTORS = 'ancestors'


def get_ancestors_from_cache(category, context):
    cache = getattr(context, CONTEXT_CACHE_NAME, None)
    if cache and CACHE_ANCESTORS in cache:
        return cache[CACHE_ANCESTORS].get(category)
    return category.get_ancestors()


class ProductAvailability(graphene.ObjectType):
    available = graphene.Boolean()
    on_sale = graphene.Boolean()
    discount = graphene.Field(Price)
    discount_local_currency = graphene.Field(Price)
    price_range = graphene.Field(PriceRange)
    price_range_undiscounted = graphene.Field(PriceRange)
    price_range_local_currency = graphene.Field(PriceRange)


class Product(DjangoObjectType):
    url = graphene.String()
    thumbnail_url = graphene.String(
        size=graphene.Argument(
            graphene.String,
            description="The size of a thumbnail, for example 255x255"))
    images = graphene.List(lambda: ProductImage)
    variants = graphene.List(lambda: ProductVariant)
    availability = graphene.Field(lambda: ProductAvailability)
    price = graphene.Field(lambda: Price)

    class Meta:
        model = models.Product
        interfaces = [relay.Node]

    def resolve_thumbnail_url(self, info, *, size=None):
        if not size:
            size = '255x255'
        return product_first_image(self, size)

    def resolve_images(self, info):
        return self.images.all()

    def resolve_variants(self, info):
        return self.variants.all()

    def resolve_url(self, info):
        return self.get_absolute_url()

    def resolve_availability(self, info):
        context = info.context
        availability = get_availability(
            self, context.discounts, context.currency)
        return ProductAvailability(**availability._asdict())


class Category(DjangoObjectType):
    products = DjangoFilterConnectionField(
        Product, filterset_class=ProductFilterSet)
    products_count = graphene.Int()
    url = graphene.String()
    ancestors = DjangoFilterConnectionField(lambda: Category)
    children = DjangoFilterConnectionField(lambda: Category)
    siblings = DjangoFilterConnectionField(lambda: Category)

    class Meta:
        model = models.Category
        filter_fields = ['id', 'name']
        interfaces = [relay.Node]

    def resolve_ancestors(self, info):
        return get_ancestors_from_cache(self, info.context)

    def resolve_children(self, info):
        return self.children.all()

    def resolve_siblings(self, info):
        return self.get_siblings()

    def resolve_products_count(self, info):
        return self.products.count()

    def resolve_url(self, info):
        ancestors = get_ancestors_from_cache(self, info.context)
        return self.get_absolute_url(ancestors)

    def resolve_products(self, info, **args):
        context = info.context
        qs = products_visible_to_user(context.user)
        qs = qs.prefetch_related('images', 'category', 'variants__stock')
        qs = qs.filter(category=self)
        return qs


class ProductVariant(DjangoObjectType):
    stock_quantity = graphene.Int()
    price_override = graphene.Field(lambda: Price)

    class Meta:
        model = models.ProductVariant
        interfaces = [relay.Node]

    def resolve_stock_quantity(self, info):
        return self.get_stock_quantity()


class ProductImage(DjangoObjectType):
    url = graphene.String(size=graphene.String())

    class Meta:
        model = models.ProductImage
        interfaces = [relay.Node]

    def resolve_url(self, info, *, size=None):
        if size:
            return self.image.crop[size].url
        return self.image.url


class ProductAttributeValue(DjangoObjectType):
    class Meta:
        model = models.AttributeChoiceValue
        interfaces = [relay.Node]


class ProductAttribute(DjangoObjectType):
    values = graphene.List(lambda: ProductAttributeValue)

    class Meta:
        model = models.ProductAttribute
        interfaces = [relay.Node]

    def resolve_values(self, info):
        return self.values.all()


def resolve_category(id, info):
    categories = models.Category.tree.filter(id=id).get_cached_trees()
    if categories:
        category = categories[0]
        cache = {CACHE_ANCESTORS: CategoryAncestorsCache(category)}
        setattr(info.context, CONTEXT_CACHE_NAME, cache)
        return category
    return None


def resolve_product(id, info):
    products = products_visible_to_user(info.context.user).filter(id=id)
    return products.first()


def resolve_attributes(category_pk):
    queryset = models.ProductAttribute.objects.prefetch_related('values')
    if category_pk:
        # Get attributes that are used with product types
        # within the given category.
        tree = models.Category.objects.get(
            pk=category_pk).get_descendants(include_self=True)
        product_types = set(
            [obj[0] for obj in models.Product.objects.filter(
                category__in=tree).values_list('product_type_id')])
        queryset = queryset.filter(
            Q(product_types__in=product_types) |
            Q(product_variant_types__in=product_types))
    return queryset.distinct()
