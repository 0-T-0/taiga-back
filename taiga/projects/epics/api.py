# -*- coding: utf-8 -*-
# Copyright (C) 2014-2016 Andrey Antukh <niwi@niwi.nz>
# Copyright (C) 2014-2016 Jesús Espino <jespinog@gmail.com>
# Copyright (C) 2014-2016 David Barragán <bameda@dbarragan.com>
# Copyright (C) 2014-2016 Alejandro Alonso <alejandro.alonso@kaleidos.net>
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from django.http import HttpResponse
from django.utils.translation import ugettext as _

from taiga.base.api.utils import get_object_or_404
from taiga.base import filters, response
from taiga.base import exceptions as exc
from taiga.base.decorators import list_route, detail_route
from taiga.base.api import ModelCrudViewSet, ModelListViewSet
from taiga.base.api.mixins import BlockedByProjectMixin
from taiga.base.utils import json

from taiga.projects.history.mixins import HistoryResourceMixin
from taiga.projects.models import Project, EpicStatus
from taiga.projects.notifications.mixins import WatchedResourceMixin, WatchersViewSetMixin
from taiga.projects.occ import OCCResourceMixin
from taiga.projects.tagging.api import TaggedResourceMixin
from taiga.projects.userstories.models import UserStory
from taiga.projects.votes.mixins.viewsets import VotedResourceMixin, VotersViewSetMixin

from . import models
from . import permissions
from . import serializers
from . import services
from . import validators
from . import utils as epics_utils


class EpicViewSet(OCCResourceMixin, VotedResourceMixin, HistoryResourceMixin,
                  WatchedResourceMixin, TaggedResourceMixin, BlockedByProjectMixin,
                  ModelCrudViewSet):
    validator_class = validators.EpicValidator
    queryset = models.Epic.objects.all()
    permission_classes = (permissions.EpicPermission,)
    filter_backends = (filters.CanViewEpicsFilterBackend,
                       filters.OwnersFilter,
                       filters.AssignedToFilter,
                       filters.StatusesFilter,
                       filters.TagsFilter,
                       filters.WatchersFilter,
                       filters.QFilter)
    filter_fields = ["project",
                     "project__slug",
                     "assigned_to",
                     "status__is_closed"]

    def get_serializer_class(self, *args, **kwargs):
        if self.action in ["retrieve", "by_ref"]:
            return serializers.EpicNeighborsSerializer

        if self.action == "list":
            return serializers.EpicListSerializer

        return serializers.EpicSerializer

    def get_queryset(self):
        qs = super().get_queryset()
        qs = qs.select_related("project",
                               "status",
                               "owner",
                               "assigned_to")

        include_attachments = "include_attachments" in self.request.QUERY_PARAMS
        qs = epics_utils.attach_extra_info(qs, user=self.request.user,
                                           include_attachments=include_attachments)

        return qs

    def pre_conditions_on_save(self, obj):
        super().pre_conditions_on_save(obj)

        if obj.status and obj.status.project != obj.project:
            raise exc.WrongArguments(_("You don't have permissions to set this status to this epic."))

    """
    Updating the epic order attribute can affect the ordering of another epics
    This method generate a key for the epic and can be used to be compared before and after
    saving
    If there is any difference it means an extra ordering update must be done
    """
    def _epics_order_key(self, obj):
        return "{}-{}".format(obj.project_id, obj.epics_order)

    def pre_save(self, obj):
        if not obj.id:
            obj.owner = self.request.user
        else:
            self._old_epics_order_key = self._epics_order_key(self.get_object())

        super().pre_save(obj)

    def _reorder_if_needed(self, obj, old_order_key, order_key, order_attr, project):
        # Executes the extra ordering if there is a difference in the  ordering keys
        if old_order_key != order_key:
            extra_orders = json.loads(self.request.META.get("HTTP_SET_ORDERS", "{}"))
            data = [{"epic_id": obj.id, "order": getattr(obj, order_attr)}]
            for id, order in extra_orders.items():
                data.append({"epic_id": int(id), "order": order})

            return services.update_epics_order_in_bulk(data,
                                                       field=order_attr,
                                                       project=project)
        return {}

    def post_save(self, obj, created=False):
        if not created:
            # Let's reorder the related stuff after edit the element
            orders_updated = self._reorder_if_needed(obj,
                                                     self._old_epics_order_key,
                                                     self._epics_order_key(obj),
                                                     "epics_order",
                                                     obj.project)
            self.headers["Taiga-Info-Order-Updated"] = json.dumps(orders_updated)

        super().post_save(obj, created)

    def update(self, request, *args, **kwargs):
        self.object = self.get_object_or_none()
        project_id = request.DATA.get('project', None)
        if project_id and self.object and self.object.project.id != project_id:
            try:
                new_project = Project.objects.get(pk=project_id)
                self.check_permissions(request, "destroy", self.object)
                self.check_permissions(request, "create", new_project)

                status_id = request.DATA.get('status', None)
                if status_id is not None:
                    try:
                        old_status = self.object.project.epic_statuses.get(pk=status_id)
                        new_status = new_project.epic_statuses.get(slug=old_status.slug)
                        request.DATA['status'] = new_status.id
                    except EpicStatus.DoesNotExist:
                        request.DATA['status'] = new_project.default_epic_status.id

            except Project.DoesNotExist:
                return response.BadRequest(_("The project doesn't exist"))

        return super().update(request, *args, **kwargs)

    @list_route(methods=["GET"])
    def filters_data(self, request, *args, **kwargs):
        project_id = request.QUERY_PARAMS.get("project", None)
        project = get_object_or_404(Project, id=project_id)

        filter_backends = self.get_filter_backends()
        statuses_filter_backends = (f for f in filter_backends if f != filters.StatusesFilter)
        assigned_to_filter_backends = (f for f in filter_backends if f != filters.AssignedToFilter)
        owners_filter_backends = (f for f in filter_backends if f != filters.OwnersFilter)

        queryset = self.get_queryset()
        querysets = {
            "statuses": self.filter_queryset(queryset, filter_backends=statuses_filter_backends),
            "assigned_to": self.filter_queryset(queryset, filter_backends=assigned_to_filter_backends),
            "owners": self.filter_queryset(queryset, filter_backends=owners_filter_backends),
            "tags": self.filter_queryset(queryset)
        }
        return response.Ok(services.get_epics_filters_data(project, querysets))

    @list_route(methods=["GET"])
    def by_ref(self, request):
        retrieve_kwargs = {
            "ref": request.QUERY_PARAMS.get("ref", None)
        }
        project_id = request.QUERY_PARAMS.get("project", None)
        if project_id is not None:
            retrieve_kwargs["project_id"] = project_id

        project_slug = request.QUERY_PARAMS.get("project__slug", None)
        if project_slug is not None:
            retrieve_kwargs["project__slug"] = project_slug

        return self.retrieve(request, **retrieve_kwargs)

    @list_route(methods=["GET"])
    def csv(self, request):
        uuid = request.QUERY_PARAMS.get("uuid", None)
        if uuid is None:
            return response.NotFound()

        project = get_object_or_404(Project, epics_csv_uuid=uuid)
        queryset = project.epics.all().order_by('ref')
        data = services.epics_to_csv(project, queryset)
        csv_response = HttpResponse(data.getvalue(), content_type='application/csv; charset=utf-8')
        csv_response['Content-Disposition'] = 'attachment; filename="epics.csv"'
        return csv_response

    @list_route(methods=["POST"])
    def bulk_create(self, request, **kwargs):
        validator = validators.EpicsBulkValidator(data=request.DATA)
        if validator.is_valid():
            data = validator.data
            project = Project.objects.get(id=data["project_id"])
            self.check_permissions(request, "bulk_create", project)
            if project.blocked_code is not None:
                raise exc.Blocked(_("Blocked element"))

            epics = services.create_epics_in_bulk(
                data["bulk_epics"],
                status_id=data.get("status_id") or project.default_epic_status_id,
                project=project,
                owner=request.user,
                callback=self.post_save, precall=self.pre_save)

            epics = self.get_queryset().filter(id__in=[i.id for i in epics])
            epics_serialized = self.get_serializer_class()(epics, many=True)

            return response.Ok(epics_serialized.data)

        return response.BadRequest(validator.errors)

    @detail_route(methods=["POST"])
    def bulk_create_related_userstories(self, request, **kwargs):
        validator = validators.CrateRelatedUserStoriesBulkValidator(data=request.DATA)
        if validator.is_valid():
            data = validator.data
            obj = self.get_object()
            project = obj.project
            self.check_permissions(request, 'bulk_create_userstories', project)
            if project.blocked_code is not None:
                raise exc.Blocked(_("Blocked element"))

            services.create_related_userstories_in_bulk(
                data["userstories"],
                obj,
                project=project,
                owner=request.user
            )
            obj = self.get_queryset().get(id=obj.id)
            epic_serialized = self.get_serializer_class()(obj)
            return response.Ok(epic_serialized.data)

        return response.BadRequest(validator.errors)

    @detail_route(methods=["POST"])
    def set_related_userstory(self, request, **kwargs):
        validator = validators.SetRelatedUserStoryValidator(data=request.DATA)
        if validator.is_valid():
            data = validator.data
            epic = self.get_object()
            project = epic.project
            user_story = UserStory.objects.get(id=data["us_id"])
            self.check_permissions(request, "update", epic)
            self.check_permissions(request, "select_related_userstory", user_story.project)

            if project.blocked_code is not None:
                raise exc.Blocked(_("Blocked element"))

            obj, created = models.RelatedUserStory.objects.update_or_create(
                epic=epic,
                user_story=user_story,
                defaults={
                    "order": data["order"]
                })
            epic = self.get_queryset().get(id=epic.id)
            epic_serialized = self.get_serializer_class()(epic)
            return response.Ok(epic_serialized.data)

        return response.BadRequest(validator.errors)

    @detail_route(methods=["POST"])
    def unset_related_userstory(self, request, **kwargs):
        validator = validators.UnsetRelatedUserStoryValidator(data=request.DATA)
        if validator.is_valid():
            data = validator.data
            epic = self.get_object()
            project = epic.project
            user_story = UserStory.objects.get(id=data["us_id"])
            self.check_permissions(request, "update", epic)

            if project.blocked_code is not None:
                raise exc.Blocked(_("Blocked element"))

            related_us = get_object_or_404(
                models.RelatedUserStory,
                epic=epic,
                user_story=user_story
            )
            related_us.delete()
            epic = self.get_queryset().get(id=epic.id)
            epic_serialized = self.get_serializer_class()(epic)
            return response.Ok(epic_serialized.data)

        return response.BadRequest(validator.errors)


class EpicVotersViewSet(VotersViewSetMixin, ModelListViewSet):
    permission_classes = (permissions.EpicVotersPermission,)
    resource_model = models.Epic


class EpicWatchersViewSet(WatchersViewSetMixin, ModelListViewSet):
    permission_classes = (permissions.EpicWatchersPermission,)
    resource_model = models.Epic