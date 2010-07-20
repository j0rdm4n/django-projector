import logging
import datetime

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.http import HttpResponseRedirect, Http404
from django.shortcuts import get_object_or_404
from django.utils.translation import ugettext as _
from django.utils.decorators import method_decorator

from projector.core.controllers import View
from projector.models import Project
from projector.forms import ProjectForm
from projector.settings import get_config_value

from vcs.web.simplevcs import settings as simplevcs_settings
from vcs.web.simplevcs.utils import get_mercurial_response, is_mercurial
from vcs.web.simplevcs.utils import log_error, basic_auth, ask_basic_auth
from vcs.web.simplevcs.exceptions import NotMercurialRequest

login_required_m = method_decorator(login_required)

class ProjectView(View):
    """
    Base class for all projector views.

    Logic should be implemented at ``response`` method.

    Would check necessary permissions defined by class attributes: ``perms``,
    ``perms_POST`` and ``perms_POST``. ``perms`` are always checked,
    ``perms_POST`` are additional checks which would be made for ``GET`` method
    requests only and ``perms_POST`` would be made for ``POST`` method requests.
    ``perms_private`` would be checked for private projects only.

    Permission attributes **must** be set at ``set_permissions`` method - for
    thread-safety we cannot set them globally at class level.
    """

    def __init__(self, request, username=None, project_slug=None, *args,
            **kwargs):
        self.request = request
        self.project = get_object_or_404(Project, slug=project_slug,
            author__username=username)
        self.author = self.project.author
        self.set_permissions()
        self.check_permissions()

    def set_permissions(self):
        self.perms = []
        self.perms_private = ['view_project']
        self.perms_GET = []
        self.perms_POST = []

    def get_required_perms(self):
        perms = self.perms
        if self.request.method == 'GET':
            perms += self.perms_GET
        if self.request.method == 'POST':
            perms += self.perms_POST
        if self.project.is_private():
            perms += self.perms_private
        perms = set(perms)
        return perms

    def check_permissions(self):
        # Owner's are always allowed to do anything with their projects
        # this would also make less database hits
        if self.project.author == self.request.user:
            return
        for perm in self.get_required_perms():
            if not self.request.user.has_perm(perm, self.project):
                if settings.DEBUG:
                    logging.info("User %s has no permission %s for project %s"
                        % (self.request.user, perm, self.project))
                raise PermissionDenied()

class ProjectDetailView(ProjectView):
    """
    Returns selected project's detail for user given in ``request``.
    We make necessary permission checks *after* dispatching between
    normal and mercurial request, as mercurial requests has it's own
    permission requirements.
    """

    template_name = 'projector/project/details.html'
    csrf_exempt = True

    def get_required_perms(self):
        if is_mercurial(self.request):
            # For mercurial requests lets undelying view to
            # manage permissions checking
            return []
        return super(ProjectDetailView, self).get_required_perms()

    def response(self, request, username, project_slug):
        try:
            if is_mercurial(request):
                return _project_detail_hg(request, self.project)
            last_part = request.path.split('/')[-1]
            if last_part and last_part != project_slug:
                raise Http404("Not a mercurial request and path longer than "
                    " should be: %s" % request.path)

            context = {
                'project': self.project,
            }
            return context
        except Exception, err:
            dont_log_exceptions = (PermissionDenied,)
            if not isinstance(err, dont_log_exceptions):
                log_error(err)
            raise err

def _project_detail_hg(request, project):
    """
    Wrapper for vcs.web.simplevcs.views.hgserve view as before we go any further
    we need to check permissions.
    TODO: Should use higher level simplevcs method
    """
    if not is_mercurial(request):
        msg = "_project_detail_hg called for non mercurial request"
        logging.error(msg)
        raise NotMercurialRequest(msg)

    if request.method not in ('GET', 'POST'):
        raise NotMercurialRequest("Only GET/POST methods are allowed, got %s"
            % request.method)
    # Allow to read from public projects
    if project.is_public() and request.method == 'GET' and \
        get_config_value('ALWAYS_ALLOW_READ_PUBLIC_PROJECTS'):
        mercurial_info = {
            'repo_path': project._get_repo_path(),
            'push_ssl': simplevcs_settings.PUSH_SSL,
        }
        return get_mercurial_response(request, **mercurial_info)

    # Check if user have been already authorized or ask to
    request.user = basic_auth(request)
    if request.user is None:
        return ask_basic_auth(request,
            realm=project.config.basic_realm)

    if project.is_private() and request.method == 'GET' and\
        not request.user.has_perm('can_read_repository', project):
        raise PermissionDenied("User %s cannot read repository for "
            "project %s" % (request.user, project))
    elif request.method == 'POST' and\
        not request.user.has_perm('can_write_to_repository',project):
        raise PermissionDenied("User %s cannot write to repository "
            "for project %s" % (request.user, project))

    mercurial_info = {
        'repo_path': project._get_repo_path(),
        'push_ssl': simplevcs_settings.PUSH_SSL,
    }

    if request.user and request.user.is_active:
        mercurial_info['allow_push'] = request.user.username

    response = get_mercurial_response(request, **mercurial_info)
    return response

class ProjectListView(View):
    template_name = 'projector/project/list.html'

    def response(self, request):
        project_list = Project.objects.for_user(user=request.user)\
            .annotate(Count('task', distinct=True))
        context = {
            'project_list' : project_list,
        }
        return context

class ProjectCreateView(View):
    """
    New project creation view.
    """

    template_name = 'projector/project/create.html'

    @login_required_m
    def response(self, request, username=None):
        # TODO: what with username param? should it be required?
        # it's not used for now...
        project = Project(
            author=request.user,
        )
        form = ProjectForm(request.POST or None, instance=project,
            initial={'public': u'private'})
        if request.method == 'POST' and form.is_valid() and \
                self.can_create(request.user, request):
            project = form.save()
            return HttpResponseRedirect(project.get_absolute_url())
        context = {
            'form' : form,
        }
        return context

    @staticmethod
    def can_create(user, request=None):
        """
        Checks if given user can create project. If
        MILIS_BETWEEN_PROJECT_CREATION is greater than miliseconds from last
        time this user has created a project then he or she is allowed to
        create new one.

        If user is trying to create more project than specified by
        MAX_PROJECTS_PER_USER configuration value then we disallow

        If request is given, send messages.
        """

        def send_error(request, message):
            if request:
                messages.error(request, message)

        try:
            date = Project.objects.filter(author=user)\
                .only('name', 'created_at')\
                .order_by('-created_at')[0].created_at
            delta = datetime.datetime.now() - date
            milis = delta.seconds * 1000
            need_to_wait = get_config_value('MILIS_BETWEEN_PROJECT_CREATION')\
                - milis
            need_to_wait /= 1000
            if need_to_wait > 0:
                send_error(request, _("You would be allowed to create a new "
                    "project in %s seconds" % need_to_wait))
                return False
        except IndexError:
            pass
        count = user.project_set.count()
        too_many = count - get_config_value('MAX_PROJECTS_PER_USER')
        if too_many > 0:
            send_error(request, _("You cannot create more projects"))
            return False
        return True

class ProjectEditView(ProjectView):
    """
    Update project view.
    """

    template_name = 'projector/project/edit.html'

    def set_permissions(self):
        super(ProjectEditView, self).set_permissions()
        self.perms = ['view_project', 'change_project']

    def response(self, request, username, project_slug):
        project = self.project
        if project.public:
            project.public = u'public'
        else:
            project.public = u'private'
        form = ProjectForm(request.POST or None, instance=project)
        if request.method == 'POST' and form.is_valid():
            project = form.save()
            message = _("Project edited successfully")
            messages.success(request, message)
            return HttpResponseRedirect(project.get_absolute_url())

        context = {
            'form' : form,
            'project': form.instance,
        }
        return context

