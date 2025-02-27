import bleach

from django import forms
from django.conf import settings

from pathlib import Path

from pontoon.base import utils
from pontoon.base.models import (
    Locale,
    ProjectLocale,
    User,
    UserProfile,
)
from pontoon.sync.formats import are_compatible_files
from pontoon.teams.utils import log_group_members


class HtmlField(forms.CharField):
    widget = forms.Textarea

    def clean(self, value):
        value = super().clean(value)
        value = bleach.clean(
            value,
            strip=True,
            tags=settings.ALLOWED_TAGS,
            attributes=settings.ALLOWED_ATTRIBUTES,
        )
        return value


class NoTabStopCharField(forms.CharField):
    widget = forms.TextInput(attrs={"tabindex": "-1"})


class NoTabStopFileField(forms.FileField):
    widget = forms.FileInput(attrs={"tabindex": "-1"})


class DownloadFileForm(forms.Form):
    slug = NoTabStopCharField()
    code = NoTabStopCharField()
    part = NoTabStopCharField()


class UploadFileForm(DownloadFileForm):
    uploadfile = NoTabStopFileField()

    def clean(self):
        cleaned_data = super().clean()
        part = cleaned_data.get("part")
        uploadfile = cleaned_data.get("uploadfile")

        if uploadfile:
            limit = 5000

            # File size validation
            if uploadfile.size > limit * 1000:
                current = round(uploadfile.size / 1000)
                message = "Upload failed. Keep filesize under {limit} kB. Your upload: {current} kB.".format(
                    limit=limit, current=current
                )
                raise forms.ValidationError(message)

            # File format validation
            if part:
                uploadfile_name = Path(uploadfile.name).name.lower()
                targetfile_name = Path(part).name.lower()

                # Fail if upload and target file are incompatible
                if not are_compatible_files(uploadfile_name, targetfile_name):
                    message = "Upload failed. File format not supported. Use {supported}.".format(
                        supported=targetfile_name
                    )
                    raise forms.ValidationError(message)


class UserPermissionLogFormMixin:
    """
    Logging of changes requires knowledge about the current user.
    We fetch information about a user from `request` object and
    log informations about changes they've made.
    """

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user")
        super().__init__(*args, **kwargs)

    def assign_users_to_groups(self, group_name, users):
        """
        Clear group membership and assign a set of users to a given group of users.
        """
        group = getattr(self.instance, f"{group_name}_group")

        add_users, remove_users = utils.get_m2m_changes(group.user_set.all(), users)

        group.user_set.clear()

        if users:
            group.user_set.add(*users)

        log_group_members(self.user, group, (add_users, remove_users))


class LocalePermsForm(UserPermissionLogFormMixin, forms.ModelForm):
    translators = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(), required=False
    )
    managers = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(), required=False
    )

    class Meta:
        model = Locale
        fields = ("translators", "managers")

    def save(self, *args, **kwargs):
        """
        Locale perms logs
        """
        translators = self.cleaned_data.get("translators", User.objects.none())
        managers = self.cleaned_data.get("managers", User.objects.none())

        self.assign_users_to_groups("translators", translators)
        self.assign_users_to_groups("managers", managers)


class ProjectLocalePermsForm(UserPermissionLogFormMixin, forms.ModelForm):
    translators = forms.ModelMultipleChoiceField(
        queryset=User.objects.all(), required=False
    )

    class Meta:
        model = ProjectLocale
        fields = ("translators", "has_custom_translators")

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)

        translators = self.cleaned_data.get("translators", User.objects.none())

        self.assign_users_to_groups("translators", translators)


class ProjectLocaleFormSet(forms.models.BaseModelFormSet):
    """
    Formset will update only existing objects and won't allow to create new-ones.
    """

    @property
    def errors_dict(self):
        errors = {}
        for form in self:
            if form.errors:
                errors[form.instance.pk] = form.errors
        return errors

    def save(self, commit=True):
        self.new_objects = []
        if commit:
            for form in self:
                if form.instance.pk and form.cleaned_data.get("has_custom_translators"):
                    form.save()

            # We have to cleanup projects from translators
            without_translators = (
                form.instance.pk
                for form in self
                if form.instance.pk
                and not form.cleaned_data.get("has_custom_translators")
            )

            if not without_translators:
                return

            ProjectLocale.objects.filter(pk__in=without_translators).update(
                has_custom_translators=False
            )

            User.groups.through.objects.filter(
                group__projectlocales__pk__in=without_translators
            ).delete()


ProjectLocalePermsFormsSet = forms.modelformset_factory(
    ProjectLocale,
    ProjectLocalePermsForm,
    formset=ProjectLocaleFormSet,
)


class UserForm(forms.ModelForm):
    """
    Form is responsible for saving user data.
    """

    first_name = forms.RegexField(
        label="Display Name",
        regex="^[^<>\"'&]+$",
        max_length=30,
        strip=True,
    )

    class Meta:
        model = User
        fields = ("first_name",)


class UserProfileForm(forms.ModelForm):
    """
    Form is responsible for saving user profile data.
    """

    class Meta:
        model = UserProfile
        fields = (
            "username",
            "contact_email",
            "bio",
            "chat",
            "github",
            "bugzilla",
        )


class UserProfileVisibilityForm(forms.ModelForm):
    """
    Form is responsible for controlling user profile visibility.
    """

    class Meta:
        model = UserProfile
        fields = (
            "visibility_email",
            "visibility_external_accounts",
            "visibility_self_approval",
            "visibility_approval",
        )


class UserCustomHomepageForm(forms.ModelForm):
    """
    Form is responsible for saving custom home page.
    """

    class Meta:
        model = UserProfile
        fields = ("custom_homepage",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        all_locales = list(Locale.objects.all().values_list("code", "name"))

        self.fields["custom_homepage"] = forms.ChoiceField(
            choices=[("", "Default homepage")] + all_locales, required=False
        )


class UserPreferredSourceLocaleForm(forms.ModelForm):
    """
    Form is responsible for saving preferred source locale
    """

    class Meta:
        model = UserProfile
        fields = ("preferred_source_locale",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        all_locales = list(Locale.objects.all().values_list("code", "name"))

        self.fields["preferred_source_locale"] = forms.ChoiceField(
            choices=[("", "Default project locale")] + all_locales, required=False
        )


class UserLocalesOrderForm(forms.ModelForm):
    """
    Form is responsible for saving preferred locales of contributor.
    """

    class Meta:
        model = UserProfile
        fields = ("locales_order",)


class GetEntitiesForm(forms.Form):
    """
    Form for parameters to the `entities` view.
    """

    project = forms.CharField()
    locale = forms.CharField()
    paths = forms.MultipleChoiceField(required=False)
    limit = forms.IntegerField(required=False, initial=50)
    status = forms.CharField(required=False)
    extra = forms.CharField(required=False)
    tag = forms.CharField(required=False)
    time = forms.CharField(required=False)
    author = forms.CharField(required=False)
    review_time = forms.CharField(required=False)
    reviewer = forms.CharField(required=False)
    exclude_self_reviewed = forms.BooleanField(required=False)
    search = forms.CharField(required=False)
    exclude_entities = forms.CharField(required=False)
    entity_ids = forms.CharField(required=False)
    pk_only = forms.BooleanField(required=False)
    inplace_editor = forms.BooleanField(required=False)
    entity = forms.IntegerField(required=False)

    def clean_paths(self):
        try:
            return self.data.getlist("paths[]")
        except AttributeError:
            # If the data source is not a QueryDict, it won't have a `getlist` method.
            return self.data.get("paths[]") or []

    def clean_limit(self):
        try:
            return int(self.cleaned_data["limit"])
        except (TypeError, ValueError):
            return 50

    def clean_search(self):
        # Return the search input as is, without any cleaning. This is in order to allow
        # users to search for strings with leading or trailing whitespaces.
        return self.data.get("search")

    def clean_exclude_entities(self):
        return utils.split_ints(self.cleaned_data["exclude_entities"])

    def clean_entity_ids(self):
        return utils.split_ints(self.cleaned_data["entity_ids"])


class AddCommentForm(forms.Form):
    """
    Form for parameters to the `add_comment` view.
    """

    locale = forms.CharField(required=False)
    entity = forms.IntegerField(required=False)
    comment = HtmlField()
    translation = forms.IntegerField(required=False)
