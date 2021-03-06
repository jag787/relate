# -*- coding: utf-8 -*-

from __future__ import division

__copyright__ = "Copyright (C) 2014 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from six.moves import intern

from django.utils.translation import (
        ugettext_lazy as _,
        pgettext,
        string_concat)
from django.shortcuts import (  # noqa
        render, get_object_or_404, redirect)
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.conf import settings
from django.urls import reverse
from django.db import transaction, IntegrityError
from django import forms
from django import http  # noqa
from django.utils import translation
from django.utils.safestring import mark_safe

from crispy_forms.layout import Submit

from course.models import (
        user_status,
        Course,
        Participation,
        ParticipationPreapproval,
        ParticipationPermission,
        ParticipationRole,
        ParticipationTag,
        participation_status)

from course.constants import (
        PARTICIPATION_PERMISSION_CHOICES,
        participation_permission as pperm,
        )

from course.auth import UserSearchWidget

from course.utils import course_view, render_course_page

from relate.utils import StyledForm, StyledModelForm

from pytools.lex import RE as REBase

# {{{ for mypy

from typing import Any, Tuple, Text, Optional  # noqa
from course.utils import CoursePageContext  # noqa

# }}}


# {{{ get_participation_for_request

def get_participation_for_request(request, course):
    # type: (http.HttpRequest, Course) -> Optional[Participation]

    # "wake up" lazy object
    # http://stackoverflow.com/questions/20534577/int-argument-must-be-a-string-or-a-number-not-simplelazyobject  # noqa
    user = request.user
    try:
        possible_user = user._wrapped
    except AttributeError:
        pass
    else:
        if isinstance(possible_user, get_user_model()):
            user = possible_user

    if not user.is_authenticated:
        return None

    participations = list(Participation.objects.filter(
            user=user,
            course=course,
            status=participation_status.active
            ))

    # The uniqueness constraint should have ensured that.
    assert len(participations) <= 1

    if len(participations) == 0:
        return None

    return participations[0]

# }}}


# {{{ get_participation_role_identifiers

def get_participation_role_identifiers(course, participation):
    # type: (Course, Optional[Participation]) -> List[Text]

    if participation is None:
        return (
                ParticipationRole.objects.filter(
                    course=course,
                    is_default_for_unenrolled=True)
                .values_list("identifier", flat=True))

    else:
        return [r.identifier for r in participation.roles.all()]

# }}}


# {{{ get_permissions

def get_participation_permissions(
        course,  # type: Course
        participation,  # type: Optional[Participation]
        ):
    # type: (...) -> frozenset[Tuple[Text, Optional[Text]]]

    if participation is not None:
        return participation.permissions()
    else:
        from course.models import ParticipationRolePermission

        perm_list = list(
                ParticipationRolePermission.objects.filter(
                    role__is_default_for_unenrolled=True)
                .values_list("permission", "argument"))

        perm = frozenset(
                (permission, argument) if argument else (permission, None)
                for permission, argument in perm_list)

        return perm

# }}}


# {{{ enrollment

@login_required
@transaction.atomic
def enroll_view(request, course_identifier):
    # type: (http.HttpRequest, str) -> http.HttpResponse

    course = get_object_or_404(Course, identifier=course_identifier)
    participation = get_participation_for_request(request, course)

    if participation is not None:
        messages.add_message(request, messages.ERROR,
                _("Already enrolled. Cannot re-renroll."))
        return redirect("relate-course_page", course_identifier)

    if not course.accepts_enrollment:
        messages.add_message(request, messages.ERROR,
                _("Course is not accepting enrollments."))
        return redirect("relate-course_page", course_identifier)

    if request.method != "POST":
        # This can happen if someone tries to refresh the page, or switches to
        # desktop view on mobile.
        messages.add_message(request, messages.ERROR,
                _("Can only enroll using POST request"))
        return redirect("relate-course_page", course_identifier)

    user = request.user
    if (course.enrollment_required_email_suffix
            and user.status != user_status.active):
        messages.add_message(request, messages.ERROR,
                _("Your email address is not yet confirmed. "
                "Confirm your email to continue."))
        return redirect("relate-course_page", course_identifier)

    preapproval = None
    if request.user.email:
        try:
            preapproval = ParticipationPreapproval.objects.get(
                    course=course, email__iexact=request.user.email)
        except ParticipationPreapproval.DoesNotExist:
            if user.institutional_id:
                if not (course.preapproval_require_verified_inst_id
                        and not user.institutional_id_verified):
                    try:
                        preapproval = ParticipationPreapproval.objects.get(
                                course=course,
                                institutional_id__iexact=user.institutional_id)
                    except ParticipationPreapproval.DoesNotExist:
                        pass
            pass

    if (
            preapproval is None
            and course.enrollment_required_email_suffix
            and not user.email.endswith(course.enrollment_required_email_suffix)):

        messages.add_message(request, messages.ERROR,
                _("Enrollment not allowed. Please use your '%s' email to "
                "enroll.") % course.enrollment_required_email_suffix)
        return redirect("relate-course_page", course_identifier)

    roles = ParticipationRole.objects.filter(
            course=course,
            is_default_for_new_participants=True)

    if preapproval is not None:
        roles = list(preapproval.roles.all())

    try:
        if course.enrollment_approval_required and preapproval is None:
            participation = handle_enrollment_request(
                    course, user, participation_status.requested,
                    roles, request)

            with translation.override(settings.RELATE_ADMIN_EMAIL_LOCALE):
                from django.template.loader import render_to_string
                message = render_to_string("course/enrollment-request-email.txt", {
                    "user": user,
                    "course": course,
                    "admin_uri": mark_safe(
                        request.build_absolute_uri(
                            reverse("relate-edit_participation",
                                args=(course.identifier, participation.id))))
                    })

                from django.core.mail import EmailMessage
                msg = EmailMessage(
                        string_concat("[%s] ", _("New enrollment request"))
                        % course_identifier,
                        message,
                        settings.ROBOT_EMAIL_FROM,
                        [course.notify_email])

                from relate.utils import get_outbound_mail_connection
                msg.connection = get_outbound_mail_connection("robot")
                msg.send()

            messages.add_message(request, messages.INFO,
                    _("Enrollment request sent. You will receive notifcation "
                    "by email once your request has been acted upon."))
        else:
            handle_enrollment_request(course, user, participation_status.active,
                                      roles, request)

            messages.add_message(request, messages.SUCCESS,
                    _("Successfully enrolled."))

    except IntegrityError:
        messages.add_message(request, messages.ERROR,
                _("A participation already exists. Enrollment attempt aborted."))

    return redirect("relate-course_page", course_identifier)


@transaction.atomic
def handle_enrollment_request(course, user, status, roles, request=None):
    # type: (Course, Any, Text, List[Text], Optional[http.HttpRequest]) -> Participation  # noqa
    participations = Participation.objects.filter(course=course, user=user)

    assert participations.count() <= 1
    if participations.count() == 0:
        participation = Participation()
        participation.user = user
        participation.course = course
        participation.status = status
        participation.save()

        if roles is not None:
            participation.roles.set(roles)
    else:
        (participation,) = participations
        participation.status = status
        participation.save()

    if status == participation_status.active:
        send_enrollment_decision(participation, True, request)
    elif status == participation_status.denied:
        send_enrollment_decision(participation, False, request)

    return participation

# }}}


# {{{ admin actions

def decide_enrollment(approved, modeladmin, request, queryset):
    count = 0

    for participation in queryset:
        if participation.status != participation_status.requested:
            continue

        if approved:
            participation.status = participation_status.active
        else:
            participation.status = participation_status.denied
        participation.save()

        send_enrollment_decision(participation, approved, request)

        count += 1

    messages.add_message(request, messages.INFO,
            # Translators: how many enroll requests have ben processed.
            _("%d requests processed.") % count)


def send_enrollment_decision(participation, approved, request=None):
    # type: (Participation, bool, http.HttpRequest) -> None

    with translation.override(settings.RELATE_ADMIN_EMAIL_LOCALE):
        course = participation.course
        if request:
            course_uri = request.build_absolute_uri(
                    reverse("relate-course_page",
                        args=(course.identifier,)))
        else:
            # This will happen when this method is triggered by
            # a model signal which doesn't contain a request object.
            from six.moves.urllib.parse import urljoin
            course_uri = urljoin(getattr(settings, "RELATE_BASE_URL"),
                                 course.get_absolute_url())

        from django.template.loader import render_to_string
        message = render_to_string("course/enrollment-decision-email.txt", {
            "user": participation.user,
            "approved": approved,
            "course": course,
            "course_uri": course_uri
            })

        from django.core.mail import EmailMessage
        msg = EmailMessage(
                string_concat("[%s] ", _("Your enrollment request"))
                % course.identifier,
                message,
                course.get_from_email(),
                [participation.user.email])
        msg.bcc = [course.notify_email]
        if not settings.RELATE_EMAIL_SMTP_ALLOW_NONAUTHORIZED_SENDER:
            from relate.utils import get_outbound_mail_connection
            msg.connection = get_outbound_mail_connection("robot")
        msg.send()


def approve_enrollment(modeladmin, request, queryset):
    decide_enrollment(True, modeladmin, request, queryset)

approve_enrollment.short_description = pgettext("Admin", "Approve enrollment")  # type:ignore  # noqa


def deny_enrollment(modeladmin, request, queryset):
    decide_enrollment(False, modeladmin, request, queryset)

deny_enrollment.short_description = _("Deny enrollment")  # type:ignore  # noqa

# }}}


# {{{ preapprovals

class BulkPreapprovalsForm(StyledForm):
    def __init__(self, course, *args, **kwargs):
        super(BulkPreapprovalsForm, self).__init__(*args, **kwargs)

        self.fields["roles"] = forms.ModelMultipleChoiceField(
                queryset=(
                    ParticipationRole.objects
                    .filter(course=course)
                    ),
                label=_("Roles"))
        self.fields["preapproval_type"] = forms.ChoiceField(
                choices=(
                    ("email", _("Email")),
                    ("institutional_id", _("Institutional ID")),
                    ),
                initial="email",
                label=_("Preapproval type"))
        self.fields["preapproval_data"] = forms.CharField(
                required=True, widget=forms.Textarea,
                help_text=_("Enter fully qualified data according to the "
                            "\"Preapproval type\" you selected, one per line."),
                label=_("Preapproval data"))

        self.helper.add_input(
                Submit("submit", _("Preapprove")))


@login_required
@transaction.atomic
@course_view
def create_preapprovals(pctx):
    if not pctx.has_permission(pperm.preapprove_participation):
        raise PermissionDenied(_("may not preapprove participation"))

    request = pctx.request

    if request.method == "POST":
        form = BulkPreapprovalsForm(pctx.course, request.POST)
        if form.is_valid():

            created_count = 0
            exist_count = 0
            pending_approved_count = 0

            roles = form.cleaned_data["roles"]
            for l in form.cleaned_data["preapproval_data"].split("\n"):
                l = l.strip()
                preapp_type = form.cleaned_data["preapproval_type"]

                if not l:
                    continue

                if preapp_type == "email":

                    try:
                        preapproval = ParticipationPreapproval.objects.get(
                                email__iexact=l,
                                course=pctx.course)
                    except ParticipationPreapproval.DoesNotExist:

                        # approve if l is requesting enrollment
                        try:
                            pending = Participation.objects.get(
                                    course=pctx.course,
                                    status=participation_status.requested,
                                    user__email__iexact=l)

                        except Participation.DoesNotExist:
                            pass

                        else:
                            pending.status = \
                                    participation_status.active
                            pending.save()
                            send_enrollment_decision(
                                    pending, True, request)
                            pending_approved_count += 1

                    else:
                        exist_count += 1
                        continue

                    preapproval = ParticipationPreapproval()
                    preapproval.email = l
                    preapproval.course = pctx.course
                    preapproval.creator = request.user
                    preapproval.save()
                    preapproval.roles.set(roles)

                    created_count += 1

                elif preapp_type == "institutional_id":

                    try:
                        preapproval = ParticipationPreapproval.objects.get(
                                course=pctx.course, institutional_id__iexact=l)

                    except ParticipationPreapproval.DoesNotExist:

                        # approve if l is requesting enrollment
                        try:
                            pending = Participation.objects.get(
                                    course=pctx.course,
                                    status=participation_status.requested,
                                    user__institutional_id__iexact=l)
                            if (
                                    pctx.course.preapproval_require_verified_inst_id
                                    and not pending.user.institutional_id_verified):
                                raise Participation.DoesNotExist

                        except Participation.DoesNotExist:
                            pass

                        else:
                            pending.status = participation_status.active
                            pending.save()
                            send_enrollment_decision(
                                    pending, True, request)
                            pending_approved_count += 1

                    else:
                        exist_count += 1
                        continue

                    preapproval = ParticipationPreapproval()
                    preapproval.institutional_id = l
                    preapproval.course = pctx.course
                    preapproval.creator = request.user
                    preapproval.save()
                    preapproval.roles.set(roles)

                    created_count += 1

            messages.add_message(request, messages.INFO,
                    _(
                        "%(n_created)d preapprovals created, "
                        "%(n_exist)d already existed, "
                        "%(n_requested_approved)d pending requests approved.")
                    % {
                        'n_created': created_count,
                        'n_exist': exist_count,
                        'n_requested_approved': pending_approved_count
                        })
            return redirect("relate-course_page", pctx.course.identifier)

    else:
        form = BulkPreapprovalsForm(pctx.course)

    return render_course_page(pctx, "course/generic-course-form.html", {
        "form": form,
        "form_description": _("Create Participation Preapprovals"),
    })

# }}}


# {{{ participation query parsing

# {{{ lexer data

_and = intern("and")
_or = intern("or")
_not = intern("not")
_openpar = intern("openpar")
_closepar = intern("closepar")

_id = intern("id")
_email = intern("email")
_email_contains = intern("email_contains")
_user = intern("user")
_user_contains = intern("user_contains")
_tagged = intern("tagged")
_role = intern("role")
_status = intern("status")
_has_started = intern("has_started")
_has_submitted = intern("has_submitted")
_whitespace = intern("whitespace")

# }}}


class RE(REBase):
    def __init__(self, s):
        # type: (str) -> None
        import re
        super(RE, self).__init__(s, re.UNICODE)


_LEX_TABLE = [
    (_and, RE(r"and\b")),
    (_or, RE(r"or\b")),
    (_not, RE(r"not\b")),
    (_openpar, RE(r"\(")),
    (_closepar, RE(r"\)")),

    # TERMINALS
    (_id, RE(r"id:([0-9]+)")),
    (_email, RE(r"email:([^ \t\n\r\f\v)]+)")),
    (_email_contains, RE(r"email-contains:([^ \t\n\r\f\v)]+)")),
    (_user, RE(r"username:([^ \t\n\r\f\v)]+)")),
    (_user_contains, RE(r"username-contains:([^ \t\n\r\f\v)]+)")),
    (_tagged, RE(r"tagged:([-\w]+)")),
    (_role, RE(r"role:(\w+)")),
    (_status, RE(r"status:(\w+)")),
    (_has_started, RE(r"has-started:([-_\w]+)")),
    (_has_submitted, RE(r"has-submitted:([-_\w]+)")),

    (_whitespace, RE("[ \t]+")),
    ]


_TERMINALS = ([
    _id, _email, _email_contains, _user, _user_contains, _tagged, _role, _status])

# {{{ operator precedence

_PREC_OR = 10
_PREC_AND = 20
_PREC_NOT = 30

# }}}


# {{{ parser

def parse_query(course, expr_str):
    from django.db.models import Q

    def parse_terminal(pstate):
        next_tag = pstate.next_tag()
        if next_tag is _id:
            result = Q(user__id=int(pstate.next_match_obj().group(1)))
            pstate.advance()
            return result

        elif next_tag is _email:
            result = Q(user__email__iexact=pstate.next_match_obj().group(1))
            pstate.advance()
            return result

        elif next_tag is _email_contains:
            result = Q(user__email__icontains=pstate.next_match_obj().group(1))
            pstate.advance()
            return result

        elif next_tag is _user:
            result = Q(user__username__exact=pstate.next_match_obj().group(1))
            pstate.advance()
            return result

        elif next_tag is _user_contains:
            result = Q(user__username__contains=pstate.next_match_obj().group(1))
            pstate.advance()
            return result

        elif next_tag is _tagged:
            ptag, created = ParticipationTag.objects.get_or_create(
                    course=course,
                    name=pstate.next_match_obj().group(1))

            result = Q(tags__pk=ptag.pk)

            pstate.advance()
            return result

        elif next_tag is _role:
            result = Q(role=pstate.next_match_obj().group(1))

            pstate.advance()
            return result

        elif next_tag is _status:
            result = Q(status=pstate.next_match_obj().group(1))

            pstate.advance()
            return result

        elif next_tag is _has_started:
            flow_id = pstate.next_match_obj().group(1)
            result = (
                    Q(flow_sessions__flow_id=flow_id)
                    & Q(flow_sessions__course=course))
            pstate.advance()
            return result

        elif next_tag is _has_submitted:
            flow_id = pstate.next_match_obj().group(1)
            result = (
                    Q(flow_sessions__flow_id=flow_id)
                    & Q(flow_sessions__course=course)
                    & Q(flow_sessions__in_progress=False))
            pstate.advance()
            return result

        else:
            pstate.expected("terminal")

    def inner_parse(pstate, min_precedence=0):
        pstate.expect_not_end()

        if pstate.is_next(_not):
            pstate.advance()
            left_query = ~inner_parse(pstate, _PREC_NOT)
        elif pstate.is_next(_openpar):
            pstate.advance()
            left_query = inner_parse(pstate)
            pstate.expect(_closepar)
            pstate.advance()
        else:
            left_query = parse_terminal(pstate)

        did_something = True
        while did_something:
            did_something = False
            if pstate.is_at_end():
                return left_query

            next_tag = pstate.next_tag()

            if next_tag is _and and _PREC_AND > min_precedence:
                pstate.advance()
                left_query = left_query & inner_parse(pstate, _PREC_AND)
                did_something = True
            elif next_tag is _or and _PREC_OR > min_precedence:
                pstate.advance()
                left_query = left_query | inner_parse(pstate, _PREC_OR)
                did_something = True
            elif (next_tag in _TERMINALS + [_not, _openpar]
                    and _PREC_AND > min_precedence):
                left_query = left_query & inner_parse(pstate, _PREC_AND)
                did_something = True

        return left_query

    from pytools.lex import LexIterator, lex
    pstate = LexIterator(
        [(tag, s, idx, matchobj)
         for (tag, s, idx, matchobj) in lex(_LEX_TABLE, expr_str, match_objects=True)
         if tag is not _whitespace], expr_str)

    if pstate.is_at_end():
        pstate.raise_parse_error("unexpected end of input")

    result = inner_parse(pstate)
    if not pstate.is_at_end():
        pstate.raise_parse_error("leftover input after completed parse")

    return result

# }}}

# }}}


# {{{ participation query

class ParticipationQueryForm(StyledForm):
    queries = forms.CharField(
            required=True,
            widget=forms.Textarea,
            help_text=string_concat(
                _("Enter queries, one per line."), " ",
                _("Allowed"), ": ",
                "<code>and</code>, "
                "<code>or</code>, "
                "<code>not</code>, "
                "<code>id:1234</code>, "
                "<code>email:a@b.com</code>, "
                "<code>email-contains:abc</code>, "
                "<code>username:abc</code>, "
                "<code>username-contains:abc</code>, "
                "<code>tagged:abc</code>, "
                "<code>role:instructor|teaching_assistant|"
                "student|observer|auditor</code>, "
                "<code>status:requested|active|dropped|denied</code>|"
                "<code>has-started:flow_id</code>|"
                "<code>has-submitted:flow_id</code>."
                ),
            label=_("Queries"))
    op = forms.ChoiceField(
            choices=(
                ("apply_tag", _("Apply tag")),
                ("remove_tag", _("Remove tag")),
                ("drop", _("Drop")),
                ),
            label=_("Operation"),
            required=True)
    tag = forms.CharField(label=_("Tag"),
            help_text=_("Tag to apply or remove"),
            required=False)

    def __init__(self, *args, **kwargs):
        super(ParticipationQueryForm, self).__init__(*args, **kwargs)

        self.helper.add_input(
                Submit("list", _("List")))
        self.helper.add_input(
                Submit("apply", _("Apply operation")))


@login_required
@transaction.atomic
@course_view
def query_participations(pctx):
    if not pctx.has_permission(pperm.query_participation):
        raise PermissionDenied(_("may not query participations"))

    request = pctx.request

    result = None

    if request.method == "POST":
        form = ParticipationQueryForm(request.POST)
        if form.is_valid():
            parsed_query = None
            try:
                for lineno, q in enumerate(form.cleaned_data["queries"].split("\n")):
                    q = q.strip()

                    if not q:
                        continue

                    parsed_subquery = parse_query(pctx.course, q)
                    if parsed_query is None:
                        parsed_query = parsed_subquery
                    else:
                        parsed_query = parsed_query | parsed_subquery

            except Exception as e:
                messages.add_message(request, messages.ERROR,
                        _("Error in line %(lineno)d: %(error_type)s: %(error)s")
                        % {
                            "lineno": lineno+1,
                            "error_type": type(e).__name__,
                            "error": str(e),
                            })

                parsed_query = None

            if parsed_query is not None:
                result = list(Participation.objects
                        .filter(course=pctx.course)
                        .filter(parsed_query)
                        .order_by("user__username")
                        .select_related("user")
                        .prefetch_related("tags"))

                if "apply" in request.POST:

                    if form.cleaned_data["op"] == "apply_tag":
                        ptag, __ = ParticipationTag.objects.get_or_create(
                                course=pctx.course, name=form.cleaned_data["tag"])
                        for p in result:
                            p.tags.add(ptag)
                    elif form.cleaned_data["op"] == "remove_tag":
                        ptag, __ = ParticipationTag.objects.get_or_create(
                                course=pctx.course, name=form.cleaned_data["tag"])
                        for p in result:
                            p.tags.remove(ptag)
                    elif form.cleaned_data["op"] == "drop":
                        for p in result:
                            p.status = participation_status.dropped
                            p.save()
                    else:
                        raise RuntimeError("unexpected operation")

                    messages.add_message(request, messages.INFO,
                            "Operation successful on %d participations."
                            % len(result))

    else:
        form = ParticipationQueryForm()

    return render_course_page(pctx, "course/query-participations.html", {
        "form": form,
        "result": result,
    })

# }}}


# {{{ edit_participation

class EditParticipationForm(StyledModelForm):
    def __init__(self, add_new, pctx, *args, **kwargs):
        # type: (bool, CoursePageContext, *Any, **Any) -> None
        super(EditParticipationForm, self).__init__(*args, **kwargs)

        participation = self.instance

        self.fields["status"].disabled = True
        self.fields["preview_git_commit_sha"].disabled = True
        self.fields["enroll_time"].disabled = True

        if not add_new:
            self.fields["user"].disabled = True

        may_edit_permissions = pctx.has_permission(pperm.edit_course_permissions)
        if not may_edit_permissions:
            self.fields["roles"].disabled = True
            # FIXME Add individual permissions

        self.fields["roles"].queryset = (
                ParticipationRole.objects.filter(
                    course=participation.course))
        self.fields["tags"].queryset = (
                ParticipationTag.objects.filter(
                    course=participation.course))

        self.fields["individual_permissions"] = forms.MultipleChoiceField(
                choices=PARTICIPATION_PERMISSION_CHOICES,
                disabled=not may_edit_permissions,
                #widget=forms.CheckboxSelectMultiple,
                help_text=_("Permissions for this participant in addition to those "
                    "granted by their role"),
                initial=self.instance.individual_permissions.values_list(
                    "permission", flat=True),
                required=False)

        self.helper.add_input(
                Submit("submit", _("Update")))
        if participation.status != participation_status.active:
            self.helper.add_input(
                    Submit("approve", _("Approve"), css_class="btn-success"))
            if participation.status == participation_status.requested:
                self.helper.add_input(
                        Submit("deny", _("Deny"), css_class="btn-danger"))
        elif participation.status == participation_status.active:
            self.helper.add_input(
                    Submit("drop", _("Drop"), css_class="btn-danger"))

    def save(self):
        # type: () -> None

        super(EditParticipationForm, self).save()

        (ParticipationPermission.objects
                .filter(participation=self.instance)
                .delete())

        pps = []
        for perm in self.cleaned_data["individual_permissions"]:
            pp = ParticipationPermission(
                        participation=self.instance,
                        permission=perm)
            pp.save()
            pps.append(pp)
        self.instance.individual_permissions.set(pps)

    class Meta:
        model = Participation
        exclude = (
                "role",
                "course",
                )

        widgets = {
                "user": UserSearchWidget,
                }


@course_view
def edit_participation(pctx, participation_id):
    # type: (CoursePageContext, int) -> http.HttpResponse
    if not pctx.has_permission(pperm.edit_participation):
        raise PermissionDenied()

    request = pctx.request

    num_participation_id = int(participation_id)

    if num_participation_id == -1:
        participation = Participation(
                course=pctx.course,
                status=participation_status.active)
        add_new = True
    else:
        participation = get_object_or_404(Participation, id=num_participation_id)
        add_new = False

    if participation.course.id != pctx.course.id:
        raise SuspiciousOperation("may not edit participation in different course")

    if request.method == 'POST':
        form = None  # type: Optional[EditParticipationForm]

        if "submit" in request.POST:
            form = EditParticipationForm(
                    add_new, pctx, request.POST, instance=participation)

            if form.is_valid():  # type: ignore
                form.save()  # type: ignore
        elif "approve" in request.POST:

            send_enrollment_decision(participation, True, pctx.request)

            participation.status = participation_status.active
            participation.save()

            messages.add_message(request, messages.SUCCESS,
                    _("Successfully enrolled."))

        elif "deny" in request.POST:
            send_enrollment_decision(participation, False, pctx.request)

            participation.status = participation_status.denied
            participation.save()

            messages.add_message(request, messages.SUCCESS,
                    _("Successfully denied."))

        elif "drop" in request.POST:
            participation.status = participation_status.dropped
            participation.save()

            messages.add_message(request, messages.SUCCESS,
                    _("Successfully dropped."))

        if form is None:
            form = EditParticipationForm(add_new, pctx, instance=participation)

    else:
        form = EditParticipationForm(add_new, pctx, instance=participation)

    return render_course_page(pctx, "course/generic-course-form.html", {
        "form_description": _("Edit Participation"),
        "form": form
        })

# }}}

# vim: foldmethod=marker
