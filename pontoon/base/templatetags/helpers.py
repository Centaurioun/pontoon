import html
import datetime
import json
import re

import markupsafe
from allauth.socialaccount import providers
from allauth.utils import get_request_param
from bleach.linkifier import Linker
from django_jinja import library
from fluent.syntax import FluentParser, ast
from fluent.syntax.serializer import serialize_expression

from django import template
from django.conf import settings
from django.contrib.humanize.templatetags import humanize
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.serializers.json import DjangoJSONEncoder
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme


register = template.Library()
parser = FluentParser()


@library.global_function
def url(viewname, *args, **kwargs):
    """Helper for Django's ``reverse`` in templates."""
    return reverse(viewname, args=args, kwargs=kwargs)


@library.global_function
def return_url(request):
    """Get an url of the previous page."""
    url = request.POST.get("return_url", request.META.get("HTTP_REFERER", "/"))
    if not url_has_allowed_host_and_scheme(url, settings.ALLOWED_HOSTS):
        return settings.SITE_URL
    return url


@library.global_function
def static(path):
    return staticfiles_storage.url(path)


@library.filter
def to_json(value):
    return json.dumps(value, cls=DjangoJSONEncoder)


@library.filter
def naturaltime(source):
    return humanize.naturaltime(source)


@library.filter
def intcomma(source):
    return humanize.intcomma(source)


@library.filter
def metric_prefix(source):
    """
    Format numbers with metric prefixes.

    Inspired by: https://stackoverflow.com/a/9462382
    """
    prefixes = [
        {"value": 1e18, "symbol": "E"},
        {"value": 1e15, "symbol": "P"},
        {"value": 1e12, "symbol": "T"},
        {"value": 1e9, "symbol": "G"},
        {"value": 1e6, "symbol": "M"},
        {"value": 1e3, "symbol": "k"},
        {"value": 1, "symbol": ""},
    ]

    for prefix in prefixes:
        if source >= prefix["value"]:
            break

    # Divide source number by the first lower prefix value
    output = source / prefix["value"]

    # Round quotient to 1 decimal point
    output = f"{output:.1f}"

    # Remove decimal point if 0
    output = output.rstrip("0").rstrip(".")

    # Append prefix symbol
    output += prefix["symbol"]

    return output


@library.filter
def comma_or_prefix(source):
    if source >= 100000:
        return metric_prefix(source)
    return humanize.intcomma(source)


@library.filter
def date_status(value, complete):
    """Get date status relative to today."""
    if isinstance(value, datetime.date):
        if not complete:
            today = datetime.date.today()
            if value <= today:
                return "overdue"
            elif (value - today).days < 8:
                return "approaching"
    else:
        return "not"

    return "normal"


@library.filter
def format_datetime(value, format="full", default="---"):
    if value is not None:
        if format == "full":
            format = "%A, %B %d, %Y at %H:%M %Z"
        elif format == "date":
            format = "%B %-d, %Y"
        elif format == "short_date":
            format = "%b %-d, %Y"
        elif format == "time":
            format = "%H:%M %Z"
        return value.strftime(format)
    else:
        return default


@library.filter
def format_timedelta(value):
    if value is not None:
        parts = []
        if value.days > 0:
            parts.append(f"{value.days} days")
        minutes = value.seconds // 60
        seconds = value.seconds % 60
        if minutes > 0:
            parts.append(f"{minutes} minutes")
        if seconds > 0:
            parts.append(f"{seconds} seconds")

        if parts:
            return ", ".join(parts)
        else:
            return "0 seconds"
    else:
        return "---"


@register.filter
@library.filter
def nospam(self):
    return markupsafe.Markup(
        html.escape(self, True).replace("@", "&#64;").replace(".", "&#46;")
    )


@library.global_function
def provider_login_url(request, provider_id=settings.AUTHENTICATION_METHOD, **query):
    """
    This function adapts the django-allauth templatetags that don't support jinja2.
    @TODO: land support for the jinja2 tags in the django-allauth.
    """
    provider = providers.registry.by_id(provider_id)

    auth_params = query.get("auth_params", None)
    process = query.get("process", None)

    if auth_params == "":
        del query["auth_params"]

    if "next" not in query:
        next_ = get_request_param(request, "next")
        if next_:
            query["next"] = next_
        elif process == "redirect":
            query["next"] = request.get_full_path()
    else:
        if not query["next"]:
            del query["next"]
    return provider.get_login_url(request, **query)


@library.global_function
def providers_media_js(request):
    """A port of django tag into jinja2"""
    return markupsafe.Markup(
        "\n".join([p.media_js(request) for p in providers.registry.get_list()])
    )


@library.filter
def pretty_url(url):
    """Remove protocol and www"""
    url = url.split("://")[1]
    if url.startswith("www."):
        url = url[4:]

    return url


@library.filter
def local_url(url, code=None):
    """Replace occurences of `{locale_code} in URL with provided code."""
    code = code or "en-US"
    return url.format(locale_code=code)


@library.filter
def dict_html_attrs(dict_obj):
    """Render json object properties into a series of data-* attributes."""
    return markupsafe.Markup(" ".join([f'data-{k}="{v}"' for k, v in dict_obj.items()]))


def _get_default_variant(variants):
    """Return default variant from the list of variants."""
    for variant in variants:
        if variant.default:
            return variant


def _serialize_value(value):
    """Serialize AST values into a simple string."""
    response = ""

    for element in value.elements:
        if isinstance(element, ast.TextElement):
            response += element.value

        elif isinstance(element, ast.Placeable):
            if isinstance(element.expression, ast.SelectExpression):
                default_variant = _get_default_variant(element.expression.variants)
                response += _serialize_value(default_variant.value)
            else:
                response += "{ " + serialize_expression(element.expression) + " }"

    return response


@library.filter
def as_simple_translation(source):
    """Transfrom complex FTL-based strings into single-value strings."""
    translation_ast = parser.parse_entry(source)

    # Non-FTL string or string with an error
    if isinstance(translation_ast, ast.Junk):
        return source

    # Value: use entire AST
    if translation_ast.value:
        tree = translation_ast

    # Attributes (must be present in valid AST if value isn't):
    # use AST of the first attribute
    else:
        tree = translation_ast.attributes[0]

    return _serialize_value(tree.value)


def is_single_input_ftl_string(source):
    """Check if fluent string is single input"""
    return get_syntax_type(source) == "simple"


def get_syntax_type(source):
    translation_ast = parser.parse_entry(source)

    if not translation_ast or not is_supported_message(translation_ast):
        return "complex"

    if is_simple_message(translation_ast) or is_simple_single_attribute_message(
        translation_ast
    ):
        return "simple"

    return "rich"


def is_supported_message(entry):
    if not entry or isinstance(entry, ast.Junk):
        return False

    attributes = entry.attributes
    value = entry.value

    if isinstance(
        entry, (ast.Junk, ast.Comment, ast.GroupComment, ast.ResourceComment)
    ):
        return False

    if value and not are_supported_elements(value.elements):
        return False

    return all(
        attr.value and are_supported_elements(attr.value.elements)
        for attr in attributes
    )


def are_supported_elements(elements):
    return all(
        is_simple_element(element)
        or (
            isinstance(element, ast.Placeable)
            and isinstance(element.expression, ast.SelectExpression)
            and all(
                all(
                    is_simple_element(variant_element)
                    for variant_element in variant.value.elements
                )
                for variant in element.expression.variants
            )
        )
        for element in elements
    )


def is_simple_message(entry):
    value = entry.value
    return (
        isinstance(entry, (ast.Message, ast.Term))
        and not entry.attributes
        and (value and all(is_simple_element(element) for element in value.elements))
    )


def is_simple_element(element):
    if isinstance(element, ast.TextElement):
        return True
    if isinstance(element, ast.Placeable):
        return isinstance(
            element.expression,
            (
                ast.FunctionReference,
                ast.TermReference,
                ast.MessageReference,
                ast.VariableReference,
                ast.NumberLiteral,
                ast.StringLiteral,
            ),
        )

    return False


def is_simple_single_attribute_message(message):
    return (
        isinstance(message, ast.Message)
        and not message.value
        and message.attributes
        and len(message.attributes) == 1
        and all(
            is_simple_element(element)
            for element in message.attributes[0].value.elements
        )
    )


def get_reconstructed_message(original, translation):
    """Return a reconstructed Fluent message from the original message and some translated content."""
    translation_ast = parser.parse_entry(original)

    if not isinstance(translation_ast, ast.Message) and not isinstance(
        translation_ast, ast.Term
    ):
        raise ValueError(f"Unexpected type in getReconstructedMessage")

    key = translation_ast.id.name
    # For Terms, the leading dash is removed in the identifier. We need to add
    # it back manually.
    if isinstance(translation_ast, ast.Term):
        key = "-" + key

    content = f"{key} ="
    indent = " " * 4

    if translation_ast.attributes and len(translation_ast.attributes) == 1:
        attribute = translation_ast.attributes[0].id.name
        content += f"\n{indent}.{attribute} ="
        indent = indent + indent

    if "\n" in translation:
        content += "\n" + re.sub(r"^", indent, translation, flags=re.MULTILINE)
    elif translation:
        content += " " + translation
    else:
        content += ' { "" }'

    return parser.parse_entry(content)


@library.filter
def linkify(source):
    """Render URLs in the string as links."""

    def set_attrs(attrs, new=False):
        attrs[(None, "target")] = "_blank"
        attrs[(None, "rel")] = "noopener noreferrer"
        return attrs

    # Escape all tags
    linker = Linker(callbacks=[set_attrs])

    return linker.linkify(source)
