from django.db import models, transaction
from django.utils import timezone, crypto
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractUser
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType

from jsonfield import JSONField

class User(AbstractUser):
    # Additional user profile data.

    notifemails_enabled = models.IntegerField(default=0, choices=[(0, "As They Happen"), (1, "Don't Email")], help_text="How often to email the user notification emails.")
    notifemails_last_notif_id = models.PositiveIntegerField(default=0, help_text="The primary key of the last notifications.Notification sent by email.")
    notifemails_last_at = models.DateTimeField(blank=True, null=True, default=None, help_text="The time when the last notification email was sent to the user.")

    # Methods

    def __str__(self):
        name = self._get_setting("name")
        if name: # question might be skipped
            return name

        # User has not entered their name.
        return self.email or "Anonymous User"

    def localize_to_org(self, org):
        # Prep this user's cached state when viewed from a particular Organization/
        self.user_settings_task = self.get_settings_task(org)
        self.can_see_org_settings = self in org.get_organization_project().get_members()

    def _get_settings_task(self):
        return getattr(self, 'user_settings_task', None)

    def _get_setting(self, key):
        # any org-localized settings?
        if not self._get_settings_task():
            return None

        # initialize cache
        if not hasattr(self, "_settings"):
            self._settings = { }

        # return from cache
        if key in self._settings:
            return self._settings[key]

        from guidedmodules.models import TaskAnswer
        ans = TaskAnswer.objects.filter(
            task=self._get_settings_task(),
            question__key=key).first()
        if ans is not None:
            ans = ans.get_current_answer()
            if ans.cleared:
                ans = None
            else:
                ans = ans.get_value()
        self._settings[key] = ans
        return ans

    def get_account_project(self, org):
        p = getattr(self, "_account_project", None)
        if p is None:
            p = self.get_account_project_(org)
            self._account_project = p
        return p

    @transaction.atomic
    def get_account_project_(self, org):
        # TODO: There's a race condition here.

        # Get an existing account project.
        pm = ProjectMembership.objects.filter(
            user=self,
            project__organization=org,
            project__is_account_project=True)\
            .select_related("project", "project__root_task", "project__root_task__module")\
            .first()
        if pm:
            return pm.project

        # Create a new one.
        from guidedmodules.models import Module, Task
        p = Project.objects.create(
            organization=org,
            title="Account Settings",
            is_account_project=True,
        )
        ProjectMembership.objects.create(
            project=p,
            user=self,
            is_admin=True)

        # Construct the root task.
        m = Module.objects.get(key="system/account/app", visible=True)
        task = Task.objects.create(
            project=p,
            editor=self,
            module=m,
            title=m.title)
        p.root_task = task
        p.save()
        return p

    @transaction.atomic
    def get_settings_task(self, org):
        p = self.get_account_project(org)
        return p.root_task.get_or_create_subtask(self, "account_settings")

    def get_profile_picture(self):
        return self._get_setting("picture")

    def render_context_dict(self, req_organization):
        organization = req_organization if isinstance(req_organization, Organization) else req_organization.organization
        profile = self.get_settings_task(organization).get_answers().as_dict()
        profile.update({
            "id": self.id,
            "fallback_avatar": self.get_avatar_fallback_css(),
        })
        if not profile.get("name"):
            profile["name"] = self.email or "Anonymous User"
        return profile


    random_colors = ('#5cb85c', '#337ab7', '#AFB', '#ABF', '#FAB', '#FBA', '#BAF', '#BFA')
    def get_avatar_fallback_css(self):
        # Compute a hash over the user ID and username to generate
        # a stable random number.
        import hashlib
        digest = hashlib.md5()
        digest.update(("%d|%s|" % (self.id, self.username)).encode("utf8"))
        digest = digest.digest()

        # Choose two colors at random using the bytes of the digest.
        color1 = User.random_colors[digest[0] % len(User.random_colors)]
        color2 = User.random_colors[digest[1] % len(User.random_colors)]

        # Generate some CSS using a gradient and a fallback style.
        return {
            "css": "background: {color1}; background: linear-gradient({color1}, {color2}); color: black;"
                .format(color1=color1, color2=color2),
            "text": self.username[0:2].upper(),
            }

from django.contrib.auth.backends import ModelBackend
class DirectLoginBackend(ModelBackend):
    # Register in settings.py!
    # Django can't log a user in without their password. In views::accept_invitation
    # we log a user in when they demonstrate ownership of an email address.
    supports_object_permissions = False
    supports_anonymous_user = False
    def authenticate(self, user_object=None):
        return user_object

class Organization(models.Model):
    name = models.CharField(max_length=256, help_text="The display name of the Organization.")
    subdomain = models.CharField(max_length=256, help_text="The subdomain of q.govready.com that this Organization exists at.")

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    updated = models.DateTimeField(auto_now=True, db_index=True)
    extra = JSONField(default={}, blank=True, help_text="Additional information stored with this object.")

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        # Return a URL with a hostname. That's unusual.
        return self.get_url()

    def get_url(self, path=''):
        # Construct the base URL of the root page of the site for this organization.
        # settings.SITE_ROOT_URL tells us the scheme, host, and port of the main Q landing site.
        # In testing it's http://localhost:8000, and in production it's https://q.govready.com.
        # Combine the scheme and port with settings.ORGANIZATION_PARENT_DOMAIN to form the
        # base URL for this Organization.
        import urllib.parse
        s = urllib.parse.urlsplit(settings.SITE_ROOT_URL)
        scheme, host = (s[0], s[1])
        port = '' if (':' not in host) else (':'+host.split(':')[1])
        return urllib.parse.urlunsplit((
            scheme, # scheme
            self.subdomain + '.' + settings.ORGANIZATION_PARENT_DOMAIN + port, # host
            path,
            '', # query
            '' # fragment
            ))

    def can_read(self, user):
        # A user can see an Organization if:
        # * they have read permission on any Project within the Organization
        # * they are an editor of a Task within a Project within the Organization (but might not otherwise be a Project member)
        # * they are a guest in any Discussion on TaskQuestion in a Task in a Project in the Organization
        from guidedmodules.models import Task
        from discussion.models import Discussion
        return \
               ProjectMembership.objects.filter(user=user, project__organization=self).exists() \
            or Task.objects.filter(editor=user, project__organization=self).exists() \
            or Discussion.objects.filter(guests=user).exists()

    def get_organization_project(self):
        prj, isnew = Project.objects.get_or_create(organization=self, is_organization_project=True,
            defaults = { "title": "Organization" })
        return prj

    def get_logo(self):
        prj_task = self.get_organization_project().root_task
        profile_task = prj_task.get_subtask("organization_profile")
        profile = profile_task.get_answers().as_dict()
        return profile.get("logo")

    @staticmethod
    def create(admin_user=None, **kargs): # admin_user is a required kwarg
        # See admin.py::OrganizationAdmin also.
        
        # Create instance by passing field values to the ORM.
        org = Organization.objects.create(**kargs)

        # And initialize the root Task of the Organization with this user as its editor.
        org.get_organization_project().set_root_task("system/organization/app", admin_user)

        # And make that user an admin of the Organization.
        ProjectMembership.objects.get_or_create(user=admin_user, project=org.get_organization_project())

        return org

class Folder(models.Model):
    """A folder is a collection of Projects."""

    organization = models.ForeignKey(Organization, related_name="folders", help_text="The Organization that this project belongs to.")

    title = models.CharField(max_length=256, help_text="The title of this Folder.")
    description = models.CharField(max_length=512, blank=True, help_text="A description of this Folder.")

    admin_users = models.ManyToManyField(User, blank=True, related_name="admin_of_folders", help_text="Users who have admin privs to the folder besides those who are admins of projects within the folder.")
    projects = models.ManyToManyField("Project", blank=True, related_name="contained_in_folders", help_text="The Projects that are listed within this Folder.")

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    updated = models.DateTimeField(auto_now=True, db_index=True)
    extra = JSONField(blank=True, help_text="Additional information stored with this object.")

    def __str__(self):
        # For the admin, notification strings
        return self.title

    def __repr__(self):
        # For debugging.
        return "<Folder %d %s>" % (self.id, self.title[0:30])

    def get_absolute_url(self):
        from django.utils.text import slugify
        return "/projects/folders/%d/%s" % (self.id, slugify(self.title))

    def get_admins(self):
        # Get all of the Users with admin privs on the folder --- which
        # is the set of users that have admin privs on any project within
        # it.
        ret = self.admin_users.all()
        for project in self.projects.all():
            ret |= project.get_admins()
        return ret.distinct()

    @staticmethod
    def get_all_folders_admin_of(user, organization):
        # Get all Folders that this user has privs to rename and add
        # new Projects to.
        return (
            Folder.objects.filter(admin_users=user)
            | Folder.objects\
            .filter(
                projects__organization=organization,
                projects__members__user=user,
                projects__members__is_admin=True))\
            .filter(organization=organization)\
            .distinct()

    def get_readable_projects(self, user):
        # Get all of the Projects within this Folder that the user can see.
        # TODO: This is not very efficient?
        return set(self.projects.all()) & set(Project.get_projects_with_read_priv(user, self.organization))

class Project(models.Model):
    """"A Project is a set of Tasks rooted in a Task whose Module's type is "project". """

    organization = models.ForeignKey(Organization, related_name="projects", help_text="The Organization that this project belongs to.")
    is_organization_project = models.NullBooleanField(default=None, help_text="Each Organization has one Project that holds Organization membership privileges and Organization settings (in its root Task). In order to have a unique_together constraint with Organization, only the values None (which need not be unique) and True (which must be unique to an Organization) are used.")

    title = models.CharField(max_length=256, help_text="The title of this Project.")

    is_account_project = models.BooleanField(default=False, help_text="Each User has one Project per Organization for account Tasks.")

        # the root_task has to be nullable because the Task itself has a non-null
        # field that refers back to this Project, and one must be NULL until the
        # other instance is created
    root_task = models.ForeignKey('guidedmodules.Task', blank=True, null=True, related_name="root_of", help_text="The root Task of this Project, which defines the structure of the Project.")

    created = models.DateTimeField(auto_now_add=True, db_index=True)
    updated = models.DateTimeField(auto_now=True, db_index=True)
    extra = JSONField(blank=True, help_text="Additional information stored with this object.")

    class Meta:
        unique_together = [('organization', 'is_organization_project')] # ensures only one can be true

    def __str__(self):
        # For the admin, notification strings
        return self.title

    def __repr__(self):
        # For debugging.
        return "<Project %d %s>" % (self.id, self.title[0:30])

    def get_absolute_url(self):
        from django.utils.text import slugify
        return "/projects/%d/%s" % (self.id, slugify(self.title))

    def organization_and_title(self):
        parts = [str(self.organization)]
        if self.is_account_project:
            parts.append(str(self.members.first().user))
            parts.append("[account settings]")
        elif self.is_organization_project:
            parts.append("[organization profile]")
        else:
            parts.append(self.title)
        return " / ".join(parts)
        
    def get_members(self):
        return User.objects.filter(projectmembership__project=self)

    def get_admins(self):
        return User.objects.filter(projectmembership__project=self, projectmembership__is_admin=True)

    def is_deletable(self):
        return not self.is_organization_project and not self.is_account_project

    def can_start_task(self, user):
        return (not self.is_account_project) and (user in self.get_members())

    def can_invite_others(self, user):
        return (not self.is_account_project) and (user in self.get_admins())

    def get_owner_domains(self):
        # Utility function for the admin/debugging to quickly see the domain
        # names in the email addresses of the admins of this project.
        return ", ".join(sorted(m.user.email.split("@", 1)[1] for m in ProjectMembership.objects.filter(project=self, is_admin=True) if m.user.email and "@" in m.user.email))

    def has_read_priv(self, user):
        # Who can see this project? Team members + anyone editing a task within
        # this project + anyone that's a guest in dicussion within this project.
        # See get_all_participants for the inverse of this function.
        from guidedmodules.models import Task
        if ProjectMembership.objects.filter(project=self, user=user).exists():
            return True
        if Task.objects.filter(editor=user, project=self, deleted_at=None).exists():
            return True
        for d in self.get_discussions_in_project_as_guest(user):
            return True
        return False

    def get_all_participants(self):
        # Get all users who have read access to this project. Inverse of
        # has_read_priv.
        from guidedmodules.models import Task, TaskAnswer
        from discussion.models import Discussion
        from collections import defaultdict

        participants = defaultdict(lambda : {
            "is_member": False,
            "is_admin": False,
            "editor_of": [],
            "discussion_guest_in": [],
        })

        # Fetch users with read access.
        for pm in ProjectMembership.objects.filter(project=self):
            participants[pm.user]["is_member"] = True
            participants[pm.user]["is_admin"] = pm.is_admin
        for task in Task.objects.filter(project=self):
            participants[task.editor]["editor_of"].append(task)
        for ta in TaskAnswer.objects.filter(task__project=self, task__deleted_at=None):
            d = Discussion.get_for(self.organization, ta)
            if d:
                for user in d.guests.all():
                    participants[user]["discussion_guest_in"].append(d)

        # Add text labels to describe user and authz.
        for user, info in participants.items():
            info["user_details"] = user.render_context_dict(self.organization)
            descr = []
            if info["is_admin"]:
                descr.append("admin")
            elif info["is_member"]:
                descr.append("member")
            if info["editor_of"]:
                descr.append("editor of %d task(s)" % len(info["editor_of"]))
            if info["discussion_guest_in"]:
                descr.append("guest in %d discussion(s)" % len(info["discussion_guest_in"]))
            info["role"] = "; ".join(descr)

        # Return sorted by username.
        return sorted(participants.items(), key = lambda kv : kv[0].username)

    @staticmethod
    def get_projects_with_read_priv(user, organization, filters={}):
        # Gets all projects a user has read priv to, excluding
        # account and organization profile projects, and sorted
        # in reverse chronological order by modified date.

        projects = set()

        if not user.is_authenticated:
            return projects

        # Add all of the Projects the user is a member of within the Organization
        # that the user is on the subdomain of.
        for pm in ProjectMembership.objects\
            .filter(project__organization=organization, user=user)\
            .filter(**{ "project__"+k: v for k, v in filters.items() })\
            .select_related('project'):
            projects.add(pm.project)
            if pm.is_admin:
                # Annotate with whether the user is an admin of the project.
                pm.project.user_is_admin = True

        # Add projects that the user is the editor of a task in, even if
        # the user isn't a team member of that project.
        from guidedmodules.models import Task
        for task in Task.get_all_tasks_readable_by(user, organization)\
            .filter(**{ "project__"+k: v for k, v in filters.items() })\
            .order_by('-created')\
            .select_related('project'):
            projects.add(task.project)

        # Add projects that the user is participating in a Discussion in
        # as a guest.
        from discussion.models import Discussion
        for d in Discussion.objects.filter(organization=organization, guests=user):
            if d.attached_to is not None: # because it is generic there is no cascaded delete and the Discussion can become dangling
                if not filters or d.attached_to.task.project in Project.objects.filter(**filters):
                    projects.add(d.attached_to.task.project)

        # Don't show system projects.
        system_projects = set(p for p in projects if p.is_organization_project or p.is_account_project)
        projects -= system_projects

        # Sort.
        projects = sorted(projects, key = lambda x : x.updated, reverse=True)

        return projects

    def primary_folder(self):
        # Weirdly, we allow in the db a Project to be in multiple Folders.
        # Return the first.
        folder = self.contained_in_folders.first()
        if folder:
            return folder

        # If the Project is not in a folder, but its root task is an answer
        # to a question, then return the primary folder of the project
        # containing that question.
        if not folder and self.root_task.is_answer_to.count():
            ans = self.root_task.is_answer_to.first()
            return ans.taskanswer.task.project.primary_folder()

        return None

    def get_parent_projects(self):
        parents = []
        project = self
        while project.root_task.is_answer_to.count():
            ans = self.root_task.is_answer_to.first()
            project = ans.taskanswer.task.project
            parents.append(project)
        parents.reverse()
        return parents

    def get_up_url(self):
        parents = self.get_parent_projects()
        if len(parents) > 0:
            return parents[-1].get_absolute_url()
        
        folder = self.primary_folder()
        if folder:
            return folder.get_absolute_url()

        return "/"

    def get_open_tasks(self, user):
        # Get all tasks that the user might want to continue working on
        # (except for the project root task).
        from guidedmodules.models import Task
        return [
            task for task in
            Task.get_all_tasks_readable_by(user, self.organization)
                .filter(project=self, editor=user) \
                .order_by('-updated')\
                .select_related('project')
            if not task.is_finished()
               and task != self.root_task ]

    def set_root_task(self, module_id, editor):
        from guidedmodules.models import Module, Task, ProjectMembership

        # create task and set it as the project root task
        if not self.id:
            raise Exception("Project must be saved first")

        # get the Module and validate that it is a project-type module
        # module_id can be either an int for a database primary key or
        # it can be a string key to specify a module ID from the YAML files
        try:
            M = Module.objects.filter(visible=True)
            if isinstance(module_id, Module):
                m = module_id
            elif isinstance(module_id, int):
                m = M.get(id=module_id)
            elif isinstance(module_id, str):
                m = M.get(key=module_id)
            else:
                raise ValueError("invalid argument")
        except Module.DoesNotExist:
            raise ValueError("invalid module id %s" % str(module_id))
        if m.spec.get("type") != "project":
            raise ValueError("invalid module")

        # create the task
        task = Task.objects.create(
            project=self,
            editor=editor,
            module=m,
            title=m.title)

        # update the project
        self.root_task = task
        self.save()

    def render_snippet(self):
        return self.root_task.render_snippet()

    def get_discussions_in_project_as_guest(self, user):
        # see has_read_priv, get_all_participants
        from discussion.models import Discussion
        for d in Discussion.objects.filter(guests=user):
            if d.attached_to.task.project == self:
                if not d.attached_to.task.deleted_at:
                    yield d
    
    def get_invitation_verb_inf(self, invitation):
        into_new_task_question_id = invitation.target_info.get('into_new_task_question_id')
        if into_new_task_question_id:
            from guidedmodules.models import ModuleQuestion
            return ("to edit a new module %s in" % ModuleQuestion.objects.get(id=into_new_task_question_id).spec["title"])
        elif invitation.target_info.get('what') == 'join-team':
            return "to join the project"
        raise ValueError()

    def get_invitation_verb_past(self, invitation):
        into_new_task_question_id = invitation.target_info.get('into_new_task_question_id')
        if into_new_task_question_id:
            pass # because .target is rewritten to the task, this never occurs
        elif invitation.target_info.get('what') == 'join-team':
            return "joined the project"
        raise ValueError()

    def is_invitation_valid(self, invitation):
        # Invitations to create a new Task remain valid so long as the
        # inviting user is a member of the project.
        return ProjectMembership.objects.filter(project=self, user=invitation.from_user).exists()

    def accept_invitation(self, invitation, add_message):
        # Create a new Task for the user to begin a module.
        into_new_task_question_id = invitation.target_info.get('into_new_task_question_id')
        if into_new_task_question_id:
            from guidedmodules.models import ModuleQuestion, Task
            mq = ModuleQuestion.objects.get(id=into_new_task_question_id)
            task = self.root_task.get_or_create_subtask(invitation.accepted_user, mq.key)

            # update the target to point to the created Task so that
            # we don't lose track of it - it will be saved into inv
            # by the caller
            invitation.target = task

            task.invitation_history.add(invitation)

        elif invitation.into_project:
            # This was handled by siteapp.views.accept_invitation.
            pass
        
        else:
            # What was this invitation for?
            raise ValueError()

    def get_invitation_redirect_url(self, invitation):
        # Just for joining a project. For accepting a task, the target
        # has been updated to that task.
        return self.get_absolute_url()

    def get_notification_watchers(self):
        return self.get_members()

    def export_json(self):
        # Exports all project data to a JSON-serializable Python data structure.
        # The caller should have administrative permissions because no authorization
        # is performed within this.

        from collections import OrderedDict

        # The Serializer class is a helper that lets us avoid serializing
        # things redundantly. Objects can asked to be serialzied once. On
        # later times, a reference to the first serialized instance is returned.
        #
        # I meant this to also prevent infinite recursion during serialization
        # but I don't think that actually works. If that's needed the logic here
        # needs to be changed.
        class Serializer:
            def __init__(self):
                self.objects = { }
                self.keys = { }
            def serializeOnce(self, object, preferred_key, serialize_func):
                if object not in self.objects:
                    # This is the first use of this object instance.
                    # Save the dict that we return for when we assign
                    # its actual key later.
                    ret = serialize_func()
                    self.objects[object] = ret
                    return ret
                else:
                    # We've already serialized this object instance once.
                    # Just refer to it on second use.
                    if object not in self.keys:
                        # This is the first re-use. Generate a key.
                        key = preferred_key
                        i = 0
                        while key in self.keys:
                            key = preferred_key + ":" + str(i)
                            i += 1
                        self.keys[object] = key
                        self.objects[object]["__referenceId__"] = key
                    return OrderedDict([
                        ("__reference__", self.keys[object]),
                    ])

        # Serialize this Project instance.
        from collections import OrderedDict
        return OrderedDict([
            ("name", "GovReady Q Project Data"), # just something human readable at the top, ignored in import
            ("schema", "1.0"), # serialization format
            ("project", OrderedDict([
                ("title", self.title),
                ("created", self.created.isoformat()), # ignored in import
                ("modified", self.updated.isoformat()), # ignored in import
                ("content", self.root_task.export_json(Serializer())),
            ])),
        ])

    def import_json(self, data, user, logger):
        # Imports project data from the 'data' value created by export_json.
        # Each answer to a question becomes a new TaskAnswerHistory entry for
        # the question, if the value has changed. For module-type questions, the
        # answers are merged recursively.
        #
        # A User must be given, who becomes the user that answered any questions
        # whose answers are saved by this import.
        #
        # Logger is a function that takes one string argument which is called
        # with any import errors or warnings.

        if not isinstance(data, dict):
            logger("Invalid data type for argument.")
            return
        
        if data.get("schema") != "1.0":
            logger("Data does not look like it was exported from this application.")
            return

        if not isinstance(data.get("project"), dict):
            logger("Data does not look like it was exported from this application.")
            return

        # Move to the project field.
        data = data["project"]

        # Create a deserialization helper.
        class Deserializer:
            def __init__(self):
                self.user = user
                self.ref_map = { }
                self.log = logger
            def deserializeOnce(self, dictdata, deserialize_func):
                # If dictdata is a reference to something we've already
                # deserialized.
                if isinstance(dictdata, dict):
                    if isinstance(dictdata.get("__reference__"), str):
                        if dictdata["__reference__"] in self.ref_map:
                            return self.ref_map[dictdata["__reference__"]]
                        else:
                            raise ValueError("Invalid __reference__: %s" % dictdata["__reference__"])

                # Otherwise, deserialize and store a reference for later.
                ret = deserialize_func()
                if isinstance(dictdata, dict):
                    if isinstance(dictdata.get("__referenceId__"), str):
                        self.ref_map[dictdata["__referenceId__"]] = ret
                        
                return ret


        # Overwrite project metadata if the fields are present, i.e. allow for
        # missing or empty fields, which will preserve the existing metadata we have.
        self.title = data.get("title") or self.title
        self.save(update_fields=["title"])

        # Update answers.
        if "content" in data:
            self.root_task.import_json_update(data["content"], Deserializer())


class ProjectMembership(models.Model):
    project = models.ForeignKey(Project, related_name="members", help_text="The Project this is defining membership for.")
    user = models.ForeignKey(User, help_text="The user that is a member of the Project.")
    is_admin = models.BooleanField(default=False, help_text="Is the user an administrator of the Project?")
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    updated = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        unique_together = [('project', 'user')]

class Invitation(models.Model):
    organization = models.ForeignKey(Organization, related_name="invitations", help_text="The Organization that this Invitation belongs to.")

    # who is sending the invitation
    from_user = models.ForeignKey(User, related_name="invitations_sent", help_text="The User who sent the invitation.")
    from_project = models.ForeignKey(Project, related_name="invitations_sent", help_text="The Project within which the invitation exists.")
    
    # what is the recipient being invited to?
    into_project = models.BooleanField(default=False, help_text="Whether the user being invited is being invited to join from_project.")
    target_content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    target_object_id = models.PositiveIntegerField()
    target = GenericForeignKey('target_content_type', 'target_object_id')
    target_info = JSONField(blank=True, help_text="Additional information about the target of the invitation.")

    # who is the recipient of the invitation?
    to_user = models.ForeignKey(User, related_name="invitations_received", blank=True, null=True, help_text="The user who the invitation was sent to, if to an existing user.")
    to_email = models.CharField(max_length=256, blank=True, null=True, help_text="The email address the invitation was sent to, if to a non-existing user.")

    # personalization
    text = models.TextField(blank=True, help_text="The personalized text of the invitation.")

    # what state is this invitation in?
    sent_at = models.DateTimeField(blank=True, null=True, help_text="If the invitation has been sent by email, when it was sent.")
    accepted_at = models.DateTimeField(blank=True, null=True, help_text="If the invitation has been accepted, when it was accepted.")
    revoked_at = models.DateTimeField(blank=True, null=True, help_text="If the invitation has been revoked, when it was revoked.")

    # what resulted from this invitation?
    accepted_user = models.ForeignKey(User, related_name="invitations_accepted", blank=True, null=True, help_text="The user that accepted the invitation (i.e. if the invitation was by email address and an account was created).")

    # random string to generate unique code for recipient
    email_invitation_code = models.CharField(max_length=64, blank=True, help_text="For emails, a unique verification code.")

    # bookkeeping
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    updated = models.DateTimeField(auto_now=True, db_index=True)
    extra = JSONField(blank=True, help_text="Additional information stored with this object.")

    def __str__(self):
        # for the admin
        return "invitation from %s to %s %s on %s" % (
            self.from_user,
            self.to_display(),
            self.purpose(),
            self.created)

    def save(self, *args, **kwargs):
        # On first save, generate the invitation code.
        if not self.id:
           self.email_invitation_code = crypto.get_random_string(24)
        super(Invitation, self).save(*args, **kwargs)

    @staticmethod
    def form_context_dict(user, project, exclude_users):
        from guidedmodules.models import ProjectMembership
        return {
            "project_id": project.id,
            "project_title": project.title,
            "users": [{ "id": pm.user.id, "name": str(pm.user) }
                for pm in ProjectMembership.objects.filter(project=project)\
                    .exclude(user__in=exclude_users)],
            "can_add_invitee_to_team": project.can_invite_others(user),
        }

    @staticmethod
    def get_for(object, open_invitations=True):
        content_type = ContentType.objects.get_for_model(object)
        ret = Invitation.objects.filter(target_content_type=content_type, target_object_id=object.id)
        if open_invitations:
            ret = ret.filter(revoked_at=None, accepted_at=None)
        return ret

    def to_display(self):
        return str(self.to_user) if self.to_user else self.to_email

    def purpose_verb(self):
        return \
              ("to join the project team and " if self.into_project and not self.is_target_the_project() else "") \
            + self.target.get_invitation_verb_inf(self)

    def is_target_the_project(self):
        return isinstance(self.target, Project)

    def purpose(self):
        return self.purpose_verb() + " " + self.target.title

    def get_acceptance_url(self):
        # The invitation must be sent using the subdomain of the organization it is
        # a part of.
        from django.core.urlresolvers import reverse
        return self.organization.get_url(reverse('accept_invitation', kwargs={'code': self.email_invitation_code}))

    def send(self):
        # Send and mark as sent.
        from htmlemailer import send_mail
        send_mail(
            "email/invitation",
            settings.DEFAULT_FROM_EMAIL,
            [self.to_user.email if self.to_user else self.to_email],
            {
                'invitation': self,
            }
        )
        Invitation.objects.filter(id=self.id).update(sent_at=timezone.now())

    def is_expired(self):
        # Return true if there is any reason why this invitation cannot be
        # accepted.

        if self.revoked_at:
            return True

        from datetime import timedelta
        if self.sent_at and timezone.now() > (self.sent_at + timedelta(days=10)):
            return True

        if not self.target.is_invitation_valid(self):
            return True

        return False
    is_expired.boolean = True

    def is_redirect_valid(self):
        return self.target.is_invitation_valid(self)

    def get_redirect_url(self):
        return self.target.get_invitation_redirect_url(self)

