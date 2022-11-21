import json
import textwrap

from datetime import datetime, timedelta
from bs4 import BeautifulSoup

from django.db.models import Model, Q
from django.utils import timezone
from django.contrib.contenttypes.models import ContentType

from rest_framework.serializers import (ModelSerializer, SerializerMethodField, CharField,
                                        IntegerField, DateTimeField, EmailField, FileField,
                                        SlugRelatedField, BooleanField, ListField)

from journal.models import Journal, Issue, IssueType
from submission.models import Article, Field, FieldAnswer, FrozenAuthor, Keyword, KeywordArticle, Section
from review.models import (ReviewForm, ReviewFormElement, ReviewRound, ReviewAssignment,
                           ReviewAssignmentAnswer, ReviewerRating, EditorAssignment,
                           RevisionRequest)
from core.models import Account, AccountRole, Country, File, Galley, Role, WorkflowElement, \
    WorkflowLog, COUNTRY_CHOICES, SALUTATION_CHOICES
from utils.models import LogEntry
from identifiers.models import Identifier
from core import files


import re

OPT_STR_FIELD = {"required": False, "allow_null": True, "allow_blank": True}
OPT_FIELD = {"required": False, "allow_null": True}


class TransporterSerializer(ModelSerializer):
    """
    Base serializer class for creating and ingesting Transporter data structures.

    Adds a number of callbacks into the validation and creation workflows, builds
    source_record_keys, parses nested route parent records, and extracts JTON foreign key values.
    """

    class Meta:
        """
        TransporterSerializer Meta classes should contain information about how to process the
        imported data.

        Additionally, it should include a field_map dict that maps Journal Transporter keys to
        Janeway model attribute names.

        The values of this dict can be used to then populate the fields attribute, needed by
        REST Framework.
        """
        # Values that are not model attributes, but must be set on a related table in postprocessing
        setting_values = {}

        # Values for which we should parse a target_record_key and look up a relation
        foreign_keys = {}

        # Default values to apply to blank or null keys
        defaults = {}

        # Fields containing HTML that should NOT be stripped
        html_fields = []

        # Each serializer requires that a model be defined
        model = None

    # All responses should output a source_record_key, a unique identifier for the created record.
    # This is used by Journal Transporter to build target_record_keys, which are in turn
    # used to reference Janeway foreign key relations.
    source_record_key = SerializerMethodField()

    def get_source_record_key(self, obj: Model) -> str:
        """Builds a source record key from model class name and model PK."""
        return "{0}:{1}".format(obj.__class__.__name__, obj.pk)

    def is_valid(self, raise_exception=False):
        """
        Before serializer validation, adds additional parsing and callbacks.

        Process:
            - Extracts foreign key values from request and possibly adds them to data.
            - Calls #before_validation callback
            - Strips HTML from text fields (except those defined in Meta.html_fields)
            - Applies default values to null or blank key values
            - Call default serializer validator

        Parameters:
            raise_exception: bool
                Passed to default validator
        """
        self.extract_foreign_keys(self.initial_data)
        self.before_validation(self.initial_data)
        self.strip_html_content(self.initial_data)
        self.apply_defaults(self.initial_data)
        return super().is_valid(raise_exception)

    def create(self, validated_data: dict, upsert: bool = False) -> Model:
        """
        Create and return a new model instance, given the validated data.

        Any model-specific logic should be encapsulated in #pre_process and #post_process.

        If this method is overridden, pre_process and post_process won't work unless invoked
        in the override.
        """
        data = validated_data
        self.apply_parent_id(data)
        self.pre_process(data)

        setting_values = self.extract_setting_values(data)
        if upsert:
            instance, _created = self.Meta.model.objects.get_or_create(**data)
        else:
            instance = self.Meta.model.objects.create(**data)

        for key, value in setting_values.items():
            if value: setattr(instance, key, value)
        instance.save()

        self.handle_attachments(instance)
        self.post_process(instance, data)

        return instance

    def extract_foreign_keys(self, data: dict):
        """
        Extract foreign keys and attempts to properly assign them to existing records.

        Foreign keys are expected to be a dictionary or list or dictionaries, each containing a
        "target_record_key" that can be parsed to extract the record's primary key.

        Parameters:
            data: dict
                The pre-validated data dict
        """
        if hasattr(self.Meta, "foreign_keys"):
            for lookup_key, foreign_key in self.Meta.foreign_keys.items():
                self.__extract_foreign_key(data, lookup_key, foreign_key)

    def __extract_foreign_key(self, data, lookup_key, foreign_key):
        """Extract a found foreign key value."""
        found = data.pop(lookup_key, None)
        if found and isinstance(found, dict):
            trk = found.pop("target_record_key", None)
            if trk:
                data[foreign_key] = self.parse_target_record_key(trk)

    @staticmethod
    def parse_target_record_key(value) -> str:
        """Parses a JTON record key to extract the local primary key."""
        return value.split(":")[-1]

    def strip_html_content(self, data: dict) -> None:
        """
        Removes any HTML from a text value, unless field key is in html_fields.

        Returns a parsed text field, attempting to preserve as much formatting as possible.

        Parameters:
            data: dict
                The pre-validated data dict, which is mutated directly
        """
        for k, v in data.items():
            if (hasattr(self.Meta, "html_fields") and k in self.Meta.html_fields):
                continue
            if not isinstance(v, str):
                continue

            soup = BeautifulSoup(v, 'html.parser')
            data[k] = soup.get_text(separator="\n")

    def apply_defaults(self, data):
        """
        Apply default values to existing dict items with null or blank values.

        Rest framework serializer defaults only work if the field is missing. This will apply
        it to any null or blank value field. Explicitly False values are permitted.

        Parameters:
            data: dict
                The pre-validated data dict.
        """
        if hasattr(self.Meta, "defaults"):
            for key, value in self.Meta.defaults.items():
                self.apply_default_value(data, key, value)

    def apply_default_value(self, data, field, default) -> None:
        """Apply default value to null or blank item."""
        if not (data.get(field) or data.get(field) is False):
            data[field] = default

    def extract_setting_values(self, data: dict) -> list:
        """
        Removes values from the data dict that are not attributes of the model.

        These values are later assigned to the model using either the model's
        setter methods (if method_name_or_lambda is a string) or
        a lambda (if method_name_or_lambda is a lambda).

        Parameters:
            data: dict
                The pre-validated data dict.
        """
        if not hasattr(self.Meta, "setting_values"): return {}

        ret = {}
        for key in self.Meta.setting_values:
            if key in data: ret[key] = data.pop(key)
        return ret

    def apply_parent_id(self, data) -> None:
        """
        Finds and attempts to apply foreign key PK values to a record based on URL nesting.

        If a relation and record are found, the PK value is assigned to the data dict, and the
        record is assigned as an instance attribute. If not, the extracted lookup value is assigned
        to an instance attribute for later use.

        Parameters:
            data: dict
                The pre-validated data dict.
        """
        viewset = self.context['view']
        parent_kwargs = viewset.get_parents_query_dict()

        for (key, value) in parent_kwargs.items():
            fk_record_name, fk_lookup_key = key.rsplit("__", 1)
            setattr(self, "{0}_{1}".format(fk_record_name, fk_lookup_key), value)

            if hasattr(self.Meta.model, fk_record_name):
                fk_model = getattr(self.Meta.model, fk_record_name).field.rel.to
                lookup = {fk_lookup_key: value}
                found = fk_model.objects.get(**lookup)

                if found:
                    setattr(self, fk_record_name, found)
                    data["{0}_id".format(fk_record_name)] = found.pk

    def handle_attachments(self, object) -> None:
        if hasattr(self.Meta, "attachments"):
            for key, attachment_name in self.Meta.attachments.items():
                metadata = self.initial_data.get(key)
                file = self.initial_data.get("{0}_file".format(key))

                if file:
                    file.name = metadata.get("name") or metadata.get("upload_name")
                    setattr(object, attachment_name, file)

            object.save()

    ############
    # Callbacks
    ############

    def before_validation(self, data: dict) -> None:
        """
        Allow serializer subclasses to adjust the data before it is validated and remapped.

        The return value of this method is not used. Mutate the data dict directly.

        This is a useful method for handling situations that might otherwise cause validation
        failure at the serializer level.

        Note that this method is called before the data dict is processed by rest framework.
        Therefore, the keys will be as-provided by Journal Transporter, and will not be mapped
        to Janeway attribute names.

        Parameters:
            data: dict
                The pre-validated data.
        """
        pass

    def pre_process(self, data: dict) -> None:
        """
        Performs any necessary pre-processing of the data dict.

        Runs after serializer validation and mapping, but before save.

        The return value of this method is not used. Mutate the data dict directly.

        If Journal Transporter values are submitted to this plugin in a format not suitable
        for saving to Janeway, this is a good place to take care of them.

        Parameters:
            data: dict
                The post-validated but uncommitted data.
        """
        pass

    def post_process(self, model: Model, data: dict) -> None:
        """
        Performs any necessary post-processing on the model after it is created.

        This method is responsible for any changes to the model, and must save it for those
        changes to persist.

        This is a good place to build dependent records that require the record created by create
        as a reference.

        Changes made to the model here will be reflected in the API response.

        Parameters:
            model: Model
                The saved record from the #create method.
            data: dict
                The post-validated data that was used to instantiate the record.
        """
        pass


class UserSerializer(TransporterSerializer):
    """
    Serializer for Transporter Users (/users).

    Maps to Janeway's core.Account model.
    """
    class Meta:
        model = Account
        field_map = {
            "source_record_key": None,
            "email": "email",
            "first_name": "first_name",
            "last_name": "last_name",
            "middle_name": "middle_name",
            "affiliation": "institution",
            "department": "department",
            "salutation": "salutation",
            "country_code": "country",
            "biography": "biography",
            "signature": "signature"
        }
        fields = tuple(field_map.keys())
        defaults = {
            "affiliation": "None"
        }

    email = EmailField()
    first_name = CharField(**OPT_STR_FIELD)
    last_name = CharField(**OPT_STR_FIELD)
    middle_name = CharField(**OPT_STR_FIELD)
    affiliation = CharField(source="institution", default="None", max_length=1000, **OPT_STR_FIELD)
    department = CharField(**OPT_STR_FIELD)
    salutation = CharField(**OPT_STR_FIELD)
    country_code = SlugRelatedField(source="country", slug_field="code", queryset=Country.objects.all(), **OPT_FIELD)
    biography = CharField(**OPT_STR_FIELD)
    signature = CharField(**OPT_STR_FIELD)

    def before_validation(self, data: dict) -> None:
        # Ensure country_code (country) is a valid code from COUNTRY_CHOICES list, else None
        if data.get("country_code"):
            matches = [code for code, country_name in COUNTRY_CHOICES
                       if data["country_code"].casefold() in [code.casefold(), country_name.casefold()]
                       ]
            data["country_code"] = matches[0] if len(matches) else None
        else:
            data["country_code"] = None

        # Ensure salutation fits SALUTATION_CHOICES list
        if data.get("salutation"):
            normalized_salutations = [re.sub("\\W", "", sal[0]).lower() for sal in SALUTATION_CHOICES]
            for salutation_tuple in SALUTATION_CHOICES:
                if re.sub("\\W", "", salutation_tuple[0]).lower() in normalized_salutations:
                    data["salutation"] = salutation_tuple[0]
                    break

    # @override
    def create(self, validated_data: dict) -> Account:
        # Do not modify existing users; return existing user (lookup by email) if present.
        try:
            existing = Account.objects.get(email=validated_data["email"].lower())
            return existing
        except Account.DoesNotExist:
            return super().create(validated_data)


class JournalSerializer(TransporterSerializer):
    """
    Transporter serializer for journals (/journals/).

    Maps to journal.models.Journal.
    """
    class Meta:
        model = Journal
        field_map = {
            "source_record_key": None,
            "path": "code",
            "title": "name",
            "description": "description",
            "online_issn": "issn",
            "print_issn": "print_issn"
        }
        fields = tuple(field_map.keys())
        setting_values = (
            "name",
            "issn",
            "print_issn"
        )
        attachments = {
            "header_file": "header_image",
            "cover_file": "default_cover_image"
        }

    path = CharField(source="code")
    title = CharField(source="name")
    description = CharField(**OPT_STR_FIELD)
    online_issn = CharField(source="issn", **OPT_STR_FIELD)
    print_issn = CharField(**OPT_STR_FIELD)

    def pre_process(self, data: dict) -> None:
        self.apply_default_value(data, "domain", "https://example.com/{0}".format(data["code"]))

    def post_process(self, journal: Journal, data: dict) -> None:
        # TODO - Need to look into importing article images if they exist
        journal.disable_article_images = True

        # Mimic regular journal creation process
        journal.setup_directory()
        # Create a default IssueType (needed for importing Issues later)
        IssueType.objects.create(journal=journal, code="issue", pretty_name="Issue")

        # Create a custom fields
        self.create_custom_fields(journal)

        journal.save()

    def create_custom_fields(self, journal: Journal) -> None:
        """
        OJS contains a number of journal and article fields that Janeway does not.
        Create non-required custom fields to capture them here.

        TODO: This is specific to OJS. Abstraction needed.
        """
        # Acknowledgements
        Field.objects.create(press=journal.press,
                             journal=journal,
                             name="acknowledgements",
                             kind="textarea",
                             required=False,
                             display=True,
                             order=0,
                             help_text="Ackowledgements"
                             )

        # Object Identifiers (other than those contained on the Article, i.e. DOI)
        Field.objects.create(press=journal.press,
                             journal=journal,
                             name="external_identifiers",
                             kind="text",
                             required=False,
                             display=False,
                             order=0,
                             help_text="External identifiers"
                             )


class JournalReviewFormSerializer(TransporterSerializer):
    """
    Transporter serializer for review forms (/journals/{id}/review_forms/).

    Maps to review.models.ReviewForm.
    """
    class Meta:
        model = ReviewForm
        field_map = {
            "source_record_key": None,
            "title": "name",
            "slug": "slug",
            "description": "intro",
            "thanks": "thanks",
            "deleted": "deleted"
        }
        fields = tuple(field_map.keys())

    title = CharField(source="name")
    slug = CharField(**OPT_STR_FIELD)
    description = CharField(source="intro", **OPT_STR_FIELD)
    thanks = CharField(**OPT_STR_FIELD)
    deleted = BooleanField(default=False)

    def before_validation(self, data: dict):
        # Reverse "active" to disabled
        if data.get("active") and not data.get("deleted"):
            data["deleted"] = not data.get("active")

    def pre_process(self, data):
        # If slug is blank, set to parameterized name.
        if not data.get("slug"):
            data["slug"] = data["name"].lower().replace(" ", "-")


class JournalReviewFormElementSerializer(TransporterSerializer):
    """
    Transporter serializer for review form elements (/journal/{id}/review_forms/{id}/elements/).

    Maps to review.models.ReviewFormElement.
    """
    class Meta:
        model = ReviewFormElement
        field_map = {
            "source_record_key": None,
            "question": "name",
            "help_text": "help_text",
            "type": "kind",
            "responses": "choices",
            "required": "required",
            "sequence": "order",
            "width": "width",
            "visible_to_author": "default_visibility"
        }
        fields = tuple(field_map.keys())

        # Mapping of JTON field types to Janeway
        # If JTON type is not found, map to text
        type_mappings = {
            "small_text": "text",
            "text": "text",
            "textarea": "textarea",
            "checkboxes": "text",  # Concat multiselect checks to string
            "checkbox": "check",
            "check": "check",
            "radio_buttons": "select"
        }
        sentence_terminators = re.compile("\\.|\\?|!")

    question = CharField(source="name")
    help_text = CharField(**OPT_STR_FIELD)
    type = CharField(source="kind")
    responses = ListField(source="choices", child=CharField(), **OPT_FIELD)
    required = BooleanField(default=False, **OPT_FIELD)
    sequence = IntegerField(source="order", default=0, **OPT_FIELD)
    width = CharField(default='large-12 columns', **OPT_STR_FIELD)
    visible_to_author = BooleanField(source="default_visibility", default=True, **OPT_FIELD)

    def pre_process(self, data: dict):
        # Map field type, default to text
        data["kind"] = self.Meta.type_mappings.get(data.get("kind")) or "text"

        # Convert choices from array to pipe-separated string
        if data.get("choices") and len(data.get("choices")):
            data["choices"] = "|".join(data.get("choices"))

        # Janeway limits the "name" (i.e. question) of each element to 200 chars.
        # Since some systems allow much longer questions, it's necessary to split longer questions
        # into "name" and "help_text".
        # As a best guess, split the name at either 200 chars, or the last sentence terminator
        # character (.|?|!) before 200 chars.
        #
        # If help_text is already defined, concat the overflow (if any) onto the front of it.
        question = data.get("name")
        help_text = data.get("help_text", "")

        # Don't assume char limit is alwasy 200 - look it up
        name_field = next(x for x in ReviewFormElement._meta.fields if x.attname == "name")
        max_length = name_field.max_length

        # If question length < max length, there's nothing to do here.
        if len(question) <= max_length or not max_length: return

        # Otherwise, find the last sentence terminator before max length
        substr = question[0:max_length]
        terminator_matches = self.Meta.sentence_terminators.search(substr)
        pruned_question_length = terminator_matches.end() if terminator_matches else max_length

        data["name"] = question[0:pruned_question_length]
        data["help_text"] = question[pruned_question_length:len(question)] + help_text

    def post_process(self, obj: ReviewFormElement, data: dict):
        # Add reference to this element to the already-existing form
        form = ReviewForm.objects.get(pk=self.review_form_id)
        if form: form.elements.add(obj)


class JournalRoleSerializer(TransporterSerializer):
    """
    Transporter serializer for user roles (/journals/{id}/roles/{user_id}/)

    Maps to core.AccountRole.

    If a role is not found, this will fail and return 400. That is intentional and expected behavior for an unmapped
    role.

    If a role maps to None, it will return 200 to avoid an error, but no role will be created.
    """
    class Meta:
        model = AccountRole
        field_map = {
            "source_record_key": None,
            "user_id": "user_id",
            "role_id": "role_id",
            "role": "role"
        }
        foreign_keys = {
            "user": "user_id"
        }
        fields = tuple(field_map.keys())
        role_map = {
            "author": "Author",
            "copyeditor": "Copyeditor",
            "editor": "Editor",
            "production_manager": "Production Manager",
            "proofing_manager": "Proofing Manager",
            "proofreader": "Proofreader",
            "reviewer": "Reviewer",
            "section_editor": "Section Editor",
            "typesetter": "Typesetter",
            "reader": None
        }

    user_id = IntegerField()
    role_id = IntegerField(**OPT_FIELD)
    role = CharField(read_only=True)

    def before_validation(self, data: dict):
        # Find the matching role record
        role = self.__find_role(data.get("role"))
        if role:
            data["role_id"] = role.pk

    def create(self, data: dict) -> Role:
        # If the role maps to None, we want to return 200 but not actually create the role
        if data.get("role_id") is None:
            return AccountRole(**data)

        # If AccountRole already exists, we can't create a new one, so just return the existing
        return super().create(data, upsert=True)

    def __find_role(self, imported_role_name: str):
        role_name = self.Meta.role_map.get(imported_role_name)
        if not role_name: return

        role = Role.objects.filter((Q(name=role_name) | Q(slug=role_name))).first()
        if not role:
            slug = role_name.lower().replace(" ", "-")
            role = Role.objects.create(name=role_name, slug=slug)

        return role


class JournalIssueSerializer(TransporterSerializer):
    """
    Transporter serializer for journal issues (/journals/{id}/issues/)

    Maps to journal.models.Issue.
    """
    class Meta:
        model = Issue
        fields = (
            "source_record_key",
            "title",
            "volume",
            "number",
            "date_published",
            "description",
            "sequence",
            "issue_type",
            "issue_type_id"
        )
        defaults = {
            "title": "Untitled Issue",
            "date_published": str(timezone.now() + timedelta(days=(365 * 50)))
        }
        attachments = {
            "cover_file": "cover_image"
        }

    title = CharField(source="issue_title", **OPT_STR_FIELD)
    volume = IntegerField(default=1, **OPT_FIELD)
    number = CharField(source="issue", default="1", **OPT_STR_FIELD)
    date_published = DateTimeField(source="date", **OPT_FIELD)
    description = CharField(source="issue_description", **OPT_STR_FIELD)
    sequence = IntegerField(source="order", **OPT_FIELD)

    issue_type = CharField(source="issue_type.code", default="issue", read_only=True)
    issue_type_id = IntegerField(write_only=True, required=False)

    def pre_process(self, data: dict) -> None:
        # We can't create a field out of issue_type, so grab it from the initial data, if it exists
        issue_type, _created = IssueType.objects.get_or_create(
            journal=self.journal,
            code=self.initial_data.get("issue_type", "issue")
        )
        data["issue_type_id"] = issue_type.pk

        # Add to end of issue order, by default
        self.apply_default_value(data, "order", len(self.journal.issues))

    def post_process(self, issue: Issue, data: dict) -> None:
        if data.get("cover_file_file"):
            issue.cover_image = data.get("cover_file_file")
            issue.save()


class JournalSectionSerializer(TransporterSerializer):
    """
    Transporter serializer for journal sections (/journals/{journal_id}/sections/)

    Maps to submission.models.Section.
    """
    class Meta:
        model = Section
        fields = (
            "source_record_key",
            "title",
            "sequence"
        )

    title = CharField(source="name", **OPT_STR_FIELD)
    sequence = IntegerField(default=0, allow_null=True)


class JournalArticleSerializer(TransporterSerializer):
    """
    Transporter serializer for journal articles (/journals/{id}/articles/)

    Maps to submission.models.Article.
    """
    class Meta:
        model = Article
        field_map = {
            "source_record_key": None,
            "title": "title",
            "abstract": "abstract",
            "language": "language",
            "date_started": "date_started",
            "date_accepted": "date_accepted",
            "date_declined": "date_declined",
            "date_submitted": "date_submitted",
            "date_published": "date_published",
            "date_updated": "date_updated",
            "status": "stage",
            "section_id": "section_id",
            "cover_letter": "comments_editor"
        }
        defaults = {
            "title": "Unknown Article"
        }
        foreign_keys = {
            "sections": "section_id"
        }
        fields = tuple(field_map.keys())
        stage_map = {
            "draft": "Unsubmitted",
            "submitted": "Unassigned",
            "assigned": "Assigned to Editor",
            "review": "Peer Review",
            "revision": "Revision",
            "rejected": "Rejected",
            "accepted": "Accepted",
            "copyediting": "Editor Copyediting",
            "typesetting": "Typesetting",
            "proofing": "Proofing",
            "published": "Published"
        }
        custom_fields = [
            "acknowledgements"
        ]

    title = CharField(**OPT_STR_FIELD)
    abstract = CharField(**OPT_STR_FIELD)
    cover_letter = CharField(source="comments_editor", **OPT_STR_FIELD)
    language = CharField(**OPT_STR_FIELD)
    date_started = DateTimeField(required=False, allow_null=True)
    date_accepted = DateTimeField(required=False, allow_null=True)
    date_declined = DateTimeField(required=False, allow_null=True)
    date_submitted = DateTimeField(required=False, allow_null=True)
    date_published = DateTimeField(required=False, allow_null=True)
    date_updated = DateTimeField(required=False, allow_null=True)
    status = CharField(source="stage", default="draft", **OPT_STR_FIELD)
    section_id = IntegerField(required=False, allow_null=True)

    def before_validation(self, data: dict) -> None:
        # Title is required
        if not data.get("title"):
            data["title"] = "Untitled Article"

        if data.get("sections") and data["sections"][0] and data["sections"][0].get("target_record_key"):
            data["section_id"] = data["sections"][0]["target_record_key"].split(":")[-1]

    def pre_process(self, data: dict) -> None:
        # Assign a default section if not otherwise defined.
        if not data.get("section_id"):
            data["section_id"] = Section.objects.get_or_create(journal=self.journal, name="Articles")[0].pk

        # Apply mapped stage
        data["stage"] = self.Meta.stage_map.get(data["stage"])
        if not data.get("stage"):
            if data.get("date_published"):
                data["stage"] = "Published"
            elif data.get("date_declined"):
                data["stage"] = "Rejected"
            else:
                data["stage"] = "Unsubmitted"

        # Ensure as many dates as possible are extrapolated to prevent defaulting to day of import
        # Default to 1/1/1900 if no date found (hopefully serves as an obvious unknown value)
        last_date = datetime(1900, 1, 1)
        for field_name in self.Meta.fields:
            if not field_name.startswith("date_"): continue

            if data.get(field_name):
                last_date = data[field_name]
            else:
                data[field_name] = last_date + timedelta(seconds=1)

    def post_process(self, model, data):
        # Assign issues (M2M)
        init_data = self.initial_data
        if init_data.get("issues") and isinstance(init_data["issues"], list):
            issues = self.initial_data.get("issues")

            for issue_dict in issues:
                pk = issue_dict.get("target_record_key").split(":")[-1]
                issue = Issue.objects.get(pk=pk)
                model.issues.add(issue)
                if not model.projected_issue: model.projected_issue_id = pk

                # Issue ordering
                # Sequence is not stored on the article, but in a separate model
                # so extract from the issue dict or article initial_data
                seq = issue_dict.get("sequence") or self.initial_data.get("sequence")
                if seq:
                    ArticleOrdering.objects.get_or_create(article=model,
                                                          issue=issue,
                                                          section=model.section,
                                                          defaults={"order": seq}
                                                          )

            model.save()

        # Assign custom field values
        self.assign_custom_field_values(model)

        # Assign Keywords
        keywords = self.initial_data.get("keywords")
        if keywords:
            self.assign_keywords(model, keywords)

        # Assign DOI
        doi = self.initial_data.get("doi")
        if doi:
            Identifier.objects.create(id_type="doi", identifier=doi, article=model)

        # Create import log entry
        external_ids = init_data.get("external_ids", "None")

        LogEntry.objects.create(
            level="Info",
            object_id=model.id,
            content_type=ContentType.objects.get(app_label="submission", model="article"),
            subject="Import",
            description=(textwrap.dedent("""\
                         Article {article_id} imported by Journal Transporter.
                         External identifiers:
                         {extids}""".format(article_id=model.id, extids=json.dumps(external_ids))))
        )

    def assign_custom_field_values(self, article: Article) -> None:
        """
        Extracts custom field values from initial_data and creates associated FieldAnswers.

        The lookup keys in initial_data are expected to be lowercased and snake_cased versions of the actual
        custom field names (i.e. field "Cool Stuff" will be looked up with key "cool_stuff"). The converter for this
        is pretty naive; it will only downcase and convert spaces to underscores.

        Parameters:
            article: Article
                The article to which the FieldAnswers should be assigned

        Returns: None
        """
        for field_name in self.Meta.custom_fields:
            answer = self.initial_data.get(field_name.lower().replace(" ", "_"))
            if answer:
                field = Field.objects.filter(journal=article.journal, name=field_name).first()
                if field:
                    FieldAnswer.objects.create(field=field, article=article, answer=answer)

    def assign_keywords(self, article: Article, keywords: list) -> None:
        if not keywords: return

        for index, keyword in enumerate(keywords):
            if keyword:
                # Keywords are capped at 200 chars. If longer, indicate the truncation with an ellipsis
                keyword = (keyword[:198] + "..") if len(keyword) > 200 else keyword
                keyword_record, _created = Keyword.objects.get_or_create(word=keyword)
                KeywordArticle.objects.get_or_create(
                    article=article,
                    keyword=keyword_record,
                    defaults={"order": (index + 1)}
                )


class JournalArticleEditorSerializer(TransporterSerializer):
    """
    Transporter serializer for article editor assignments (/journals/{id}/articles/{id}/editors).

    Maps to submittion.editor_assignment.
    """
    class Meta:
        model = EditorAssignment
        field_map = {
            "source_record_key": None,
            "notified": "notified",
            "date_notified": "assigned",
            "editor_type": "editor_type",
            "editor_id": "editor_id"
        }
        foreign_keys = {
            "editor": "editor_id"
        }
        defaults = {
            "editor_type": "editor"
        }
        fields = tuple(field_map.keys())

    notified = BooleanField(**OPT_FIELD)
    date_notified = DateTimeField(source="assigned", **OPT_FIELD)
    editor_type = CharField(default="editor", **OPT_STR_FIELD)

    editor_id = IntegerField()

    def pre_process(self, data: dict):
        data["notified"] = bool(data.get("date_notified"))
        data["assigned"] = data.get("assigned") or self.article.date_submitted or datetime.now()


class JournalArticleAuthorSerializer(UserSerializer):
    """
    Transporter serializer for article authors (/journals/{id}/articles/{id}/authors).

    Maps to submission.models.FrozenAuthor.

    If an email address is provided, this serializer will attempt to find an Account by that email
    and attach the author to the frozen author.
    """
    class Meta(UserSerializer.Meta):
        model = FrozenAuthor
        field_map = {
            "source_record_key": None,
            "email": "frozen_email",
            "first_name": "first_name",
            "last_name": "last_name",
            "middle_name": "middle_name",
            "affiliation": "institution",
            "department": "department",
            "salutation": "name_prefix",
            "country_code": "country",
            "order": "order",
            "primary_contact": None,
            "user_id": "author_id"
        }
        defaults = {
            "department": "None"
        }
        foreign_keys = {
            "user": "user_id"
        }
        fields = tuple(field_map.keys())

    email = EmailField(source="frozen_email", **OPT_STR_FIELD)
    first_name = CharField(**OPT_STR_FIELD)
    last_name = CharField(**OPT_STR_FIELD)
    middle_name = CharField(**OPT_STR_FIELD)
    affiliation = CharField(source="institution", default="None", max_length=1000, **OPT_STR_FIELD)
    department = CharField(**OPT_STR_FIELD)
    salutation = CharField(source="name_prefix", **OPT_STR_FIELD)
    country_code = SlugRelatedField(source="country", slug_field="code", queryset=Country.objects.all(), **OPT_FIELD)
    biography = CharField(**OPT_STR_FIELD)
    signature = CharField(**OPT_STR_FIELD)
    primary_contact = SerializerMethodField()

    user_id = IntegerField(source="author_id", **OPT_FIELD)

    def create(self, validated_data: dict) -> FrozenAuthor:
        viewset = self.context['view']
        kwargs = viewset.get_parents_query_dict()

        self.article = self.article if hasattr(self, "article") else Article.objects.get(pk=kwargs["article__id"])
        frozen_author = FrozenAuthor.objects.create(article=self.article, **validated_data)

        self.post_process(frozen_author, validated_data)

        return frozen_author

    def post_process(self, record: FrozenAuthor, data: dict):
        if record.author:
            self.article.authors.add(record.author)
            # Primary contact is not a model attr, so look it up in the initial (unvalidated) data.
            if self.initial_data.get("primary_contact"):
                self.article.correspondence_author = record.author
            self.article.save()

    def get_primary_contact(self, record: FrozenAuthor):
        return record.is_correspondence_author


class JournalArticleFileSerializer(TransporterSerializer):
    """
    Transporter serializer for article files (/journals/{id}/arti8cles/{id}/files).

    Maps to core.models.File and attaches to appropriate Article. Also handles building
    file history if parent file is defined.
    """
    class Meta:
        model = File
        field_map = {
            "source_record_key": None,
            "file": None,
            "date_uploaded": "date_uploaded",
            "description": "description",
            "label": "label",
            "original_filename": "original_filename",
            "is_galley_file": "is_galley",
            "is_supplementary_file": None
        }
        fields = tuple(field_map.keys())

    file = FileField(write_only=True, use_url=False, allow_empty_file=True)
    date_uploaded = DateTimeField(**OPT_FIELD)
    description = CharField(**OPT_STR_FIELD)
    label = CharField(**OPT_STR_FIELD)
    original_filename = CharField(max_length=1000, **OPT_STR_FIELD)
    is_galley_file = BooleanField(source="is_galley", default=False)

    is_supplementary_file = BooleanField(read_only=True, default=False)

    def create(self, validated_data: dict) -> Model:
        self.article = self.get_article()

        self.pre_process(validated_data)

        raw_file = validated_data.pop("file")

        # If the file has a parent, then it belongs in the file history
        if self.initial_data.get("parent_target_record_key"):
            replaced_file_pk = self.initial_data.get("parent_target_record_key").split(":")[-1]
            replaced_file = File.objects.get(pk=replaced_file_pk)
            if replaced_file:
                # TODO: Is this the best way to do this? Is "overwriting" correct?
                file = files.overwrite_file(raw_file, replaced_file, (self.article, self.article.pk))
        else:
            file = files.save_file_to_article(raw_file,
                                              self.article,
                                              None,
                                              validated_data.get("label") or validated_data.get("original_filename"),
                                              description=validated_data.get("description"),
                                              is_galley=(validated_data.get("is_galley") or False)
                                              )

        self.post_process(file, validated_data)
        return file

    def post_process(self, record: File, data: dict):
        if data.get("is_galley"):
            Galley.objects.create(article=self.article, file=record)

        if data.get("is_supplementary_file"):
            self.article.supplementary_files.add(record)
        elif not data.get("parent_target_record_key"):
            self.article.manuscript_files.add(record)

        if data.get("date_uploaded"):
            record.date_uploaded = data.get("date_uploaded")

        record.save()

    def get_article(self) -> Article:
        # The File model's article reference is not a foreign key, so regular parent
        # look ups won't work. Get the artice here.
        viewset = self.context['view']
        kwargs = viewset.get_parents_query_dict()

        return Article.objects.get(pk=kwargs["article__id"])


class JournalArticleLogEntrySerializer(TransporterSerializer):
    """
    Transporter serializer for article log entries (/journals/{id}/articles/{id}/logs/).

    Maps to utils.models.LogEntry.
    """
    class Meta:
        model = LogEntry
        field_map = {
            "source_record_key": None,
            "date": "date",
            "title": "subject",
            "description": "description",
            "level": "level",
            "ip_address": "ip_address",
            "user": "actor_id"
        }
        fields = tuple(field_map.keys())
        foreign_keys = {
            "user": "user"
        }
        log_level_map = {
            "notice": "Info",
            "debug": "Debug",
            "warn": "Error",
            "error": "Error",
            "fatal": "Error"
        }

    user = IntegerField(source="actor_id", **OPT_FIELD)

    date = DateTimeField()
    title = CharField(source="subject", **OPT_STR_FIELD)
    description = CharField(**OPT_STR_FIELD)
    level = CharField(**OPT_STR_FIELD)
    ip_address = CharField(**OPT_STR_FIELD)

    def pre_process(self, data: dict) -> None:
        # Map log level
        data["level"] = self.Meta.log_level_map.get(data.get("level")) or "Info"

        # Map article
        data["object_id"] = self.article_id

    def post_process(self, log_entry: LogEntry, data: dict) -> None:
        # Set content type to article
        log_entry.content_type = ContentType.objects.get(app_label="submission", model="article")
        # Override #date's auto_now_add
        log_entry.date = data["date"]
        log_entry.save()


class JournalArticleRevisionRequestSerializer(TransporterSerializer):
    """
    Transporter serializer for article revision requests (/journals/{id}/articles/{id}/revision_requests/).

    Maps to review.models.RevisionRequest.
    """
    class Meta:
        model = RevisionRequest
        field_map = {
            "source_record_key": None,
            "comment": "editor_note",
            "author_comment": "author_note",
            "decision": "type",
            "date": "date_requested",
            "date_due": "date_due",
            "date_completed": "date_completed",
            "editor_id": "editor_id"
        }
        fields = tuple(field_map.keys())
        foreign_keys = {
            "article": "article_id",
            "editor": "editor_id"
        }
        type_map = {
            "revisions": "minor_revisions"
        }

    editor_id = IntegerField()

    decision = CharField(source="type")
    comment = CharField(source="editor_note", default="None", **OPT_STR_FIELD)
    author_comment = CharField(source="author_note", **OPT_STR_FIELD)
    date = DateTimeField(source="date_requested", **OPT_FIELD)
    date_due = DateTimeField(**OPT_FIELD)
    date_completed = DateTimeField(**OPT_FIELD)

    def pre_process(self, data: dict) -> None:
        data["type"] = self.Meta.type_map.get(data.get("type")) or "minor_revisions"
        self.apply_default_value(data, "date_due", data.get("date_requested") + timedelta(days=30))


class JournalArticleRoundSerializer(TransporterSerializer):
    """
    Transporter serializer for article rounds (/journals/{id}/articles/{id}/rounds/).

    Maps to review.models.ReviewRound.
    """
    class Meta:
        model = ReviewRound
        field_map = {
            "source_record_key": None,
            "round": "round_number",
            "date": "date_started"
        }
        fields = tuple(field_map.keys())

    round = IntegerField(source="round_number")
    date = DateTimeField(source="date_started", **OPT_FIELD)

    def post_process(self, record: ReviewRound, data: dict):
        if record.round_number == 1:
            workflow_element = WorkflowElement.objects.get(journal_id=self.journal_id, element_name="review")
            existing = WorkflowLog.objects.filter(article=record.article, element=workflow_element)
            if not existing:
                WorkflowLog.objects.create(article=record.article,
                                           element=workflow_element,
                                           timestamp=record.date_started)


class JournalArticleRoundAssignmentSerializer(TransporterSerializer):
    """
    Transporter serializer for Review Assignments (/journals/{id}/articles/{id}/rounds/{id}/assignments/).

    Maps to review.models.ReviewAssignment.
    """
    class Meta:
        model = ReviewAssignment
        foreign_keys = {
            "reviewer": "reviewer_id",
            "editor": "editor_id",
            "review_file": "review_file_id",
            "review_form": "review_form_id"
        }
        field_map = {
            "source_record_key": None,
            "recommendation": "decision",
            "date_requested": "date_assigned",
            "date_due": "date_due",
            "date_confirmed": "date_accepted",
            "date_declined": "date_declined",
            "date_completed": "date_complete",
            "date_reminded": "date_reminded",
            "responded": "is_complete",
            "comments": "comments_for_editor",
            "quality": "rating",
            "has_response": "is_complete",
            "reviewer_id": "reviewer_id",
            "editor_id": "editor_id",
            "review_file_id": "review_file_id",
            "review_form_id": "review_form_id",
        }
        decision_map = {
            "accept": "accept",
            "pending_revisions": "minor_revisions",
            "resubmit_here": "major_revisions",
            "see_comments": "major_revisions",
            "resubmit_elsewhere": "reject",
            "decline": "reject"
        }
        visibility_map = {
            "open": "open",
            "blind": "blind",
            "double_blind": "double-blind"
        }
        fields = tuple(field_map.keys())

    recommendation = CharField(source="decision", **OPT_STR_FIELD)
    responded = BooleanField(source="is_complete", **OPT_FIELD)
    comments = CharField(source="comments_for_editor", **OPT_STR_FIELD)
    has_response = BooleanField(source="is_complete", **OPT_FIELD)
    date_due = DateTimeField()
    date_confirmed = DateTimeField(source="date_accepted", **OPT_FIELD)
    date_declined = DateTimeField(**OPT_FIELD)
    date_completed = DateTimeField(source="date_complete", **OPT_FIELD)
    date_reminded = DateTimeField(**OPT_FIELD)

    reviewer_id = IntegerField(**OPT_FIELD)
    editor_id = IntegerField(**OPT_FIELD)
    review_file_id = IntegerField(**OPT_FIELD)
    review_form_id = IntegerField(source="form_id", **OPT_FIELD)

    quality = SerializerMethodField()

    def get_quality(self, obj: ReviewAssignment):
        rating = obj.review_rating
        return (rating.rating * 10) if rating else None

    def before_validation(self, data: dict):
        if not data.get("date_due"):
            data["date_due"] = data.get("date_completed") or data.get("date_assigned")
        comment = data.get("comments")
        data["comments"] = comment[0]["comments"] if isinstance(comment, list) and len(comment) > 0 else None

    def pre_process(self, data: dict):
        # Map decision
        if data.get("decision"):
            normalized_decision = data.get("decision").replace(" ", "_").lower()
            data["decision"] = self.Meta.decision_map.get(normalized_decision) or data.get("decision")

    def post_process(self, record: ReviewAssignment, data: dict):
        # Build review rating, which comes in as a value between 0-100
        quality = self.initial_data.get("quality")
        if quality and record.editor:
            ReviewerRating.objects.create(assignment=record, rater=record.editor, rating=(quality * 10))

        if record.review_file and not record.review_round.review_files.filter(pk=record.review_file.pk).exists():
            record.review_round.review_files.add(record.review_file)


class JournalArticleRoundAssignmentResponseSerializer(TransporterSerializer):
    """
    Transporter serializer for review assignment responses
    (journals/{id}/articles/{id}/rounds/{id}/assignments{id}/response/).

    Maps to review.models.ReviewAssignmentAnswer.
    """
    class Meta:
        model = ReviewAssignmentAnswer
        field_map = {
            "source_record_key": None,
            "response_value": "answer",
            "review_form_element_id": "original_element_id",
            "visible_to_author": "author_can_see"
        }
        foreign_keys = {
            "review_form_element": "review_form_element_id"
        }
        fields = tuple(field_map.keys())

    response_value = CharField(source="answer", **OPT_STR_FIELD)
    review_form_element_id = IntegerField(source="original_element_id")
    visible_to_author = BooleanField(source="author_can_see", default=False, **OPT_FIELD)

    def before_validation(self, data: dict):
        if data.get("response_value") and isinstance(data.get("response_value"), list):
            data["response_value"] = "; ".join(data.get("response_value"))

    def post_process(self, model: ReviewAssignmentAnswer, data: dict):
        model.original_element.snapshot(model)
