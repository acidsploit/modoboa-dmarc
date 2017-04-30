"""DMARC views."""

import collections
import concurrent.futures
import datetime
import tldextract

from dns import resolver, reversename

from django.db.models import Q
from django.utils import timezone
from django.views import generic

from django.contrib.auth import mixins as auth_mixins
from django.utils.translation import ugettext_lazy as _

from modoboa.admin import models as admin_models
from modoboa.parameters import tools as param_tools

from . import models


def insert_record(target, record, name):
    """Add a record."""

    if name not in target:
        target[name] = {}

    if record.source_ip not in target[name]:
        target[name][record.source_ip] = {
            "total": 0,
            "spf": {"pass": 0, "fail": 0},
            "dkim": {"pass": 0, "fail": 0}
        }
    target[name][record.source_ip]["total"] += record.count
    for typ in ["spf", "dkim"]:
        result = getattr(record, "{}_result".format(typ))
        target[name][record.source_ip][typ][result] += record.count


def week_range(year, weeknumber):
    """Return start and end dates of a given week."""
    fmt = "%Y-%W-%w"
    start_week = datetime.datetime.strptime(
        "{}-{}-{}".format(year, weeknumber, 1), fmt)
    end_week = datetime.datetime.strptime(
        "{}-{}-{}".format(year, weeknumber, 0), fmt)
    return start_week, end_week


class DomainReportView(
        auth_mixins.PermissionRequiredMixin,
        generic.TemplateView):
    """ListView for Report."""

    permission_required = "modoboa_dmarc.view_report"
    template_name = "modoboa_dmarc/domain_report.html"

    def get_queryset(self):
        """Filter reports."""
        self.period = self.request.GET.get("period", "")
        if not self.period:
            year, week, day = timezone.now().isocalendar()
            week -= 1
            self.period = "{}-{}".format(year, week)
        else:
            year, week = self.period.split("-")

        self.daterange = week_range(year, week)
        self.domain = admin_models.Domain.objects.get(pk=self.kwargs["pk"])
        qargs = (
            (Q(report__start_date__gte=self.daterange[0],
               report__start_date__lte=self.daterange[1]) |
             Q(report__end_date__gte=self.daterange[0],
               report__end_date__lte=self.daterange[1])) &
            Q(header_from=self.domain)
        )
        return models.Record.objects.select_related().filter(qargs)

    def get_context_data(self, *args, **kwargs):
        """Extra context data."""
        context = super(DomainReportView, self).get_context_data(
            *args, **kwargs)
        qset = self.get_queryset()
        stats = {
            "total": 0,
            "aligned": 0,
            "trusted": 0,
            "failed": 0
        }
        stats_per_day = collections.OrderedDict()
        step = datetime.timedelta(days=1)
        iterator = self.daterange[0]
        while iterator <= self.daterange[1]:
            stats_per_day[iterator.date()] = {}
            iterator += step
        trusted = collections.OrderedDict()
        threats = collections.OrderedDict()

        all_records = qset.all()
        dns_names = {}
        if param_tools.get_global_parameter("enable_rlookups"):
            dns_resolver = resolver.Resolver()
            dns_resolver.timeout = 1.0;
            dns_resolver.lifetime = 1.0;

            def get_domain_name_from_ip(ip):
                addr = reversename.from_address(ip)
                try:
                    resp = dns_resolver.query(addr, "PTR")
                    ext = tldextract.extract(str(resp[0].target))
                    return (ip, '.'.join((ext.domain, ext.suffix)).lower())
                except (resolver.NXDOMAIN, resolver.YXDOMAIN, resolver.NoAnswer,
                        resolver.NoNameservers, resolver.Timeout):
                    return (None, None)

            ips = (r.source_ip for r in all_records)
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                dns_names = {i: n for (i, n) in
                             list(pool.map(get_domain_name_from_ip, ips))}

        for record in all_records:
            stats["total"] += record.count
            name = dns_names.get(record.source_ip, _("Not resolved"))
            if record.dkim_result == "pass" and record.spf_result == "pass":
                stats["aligned"] += record.count
                stats["trusted"] += record.count
                insert_record(trusted, record, name)
            elif record.dkim_result == "pass" or record.spf_result == "pass":
                stats["trusted"] += record.count
                insert_record(trusted, record, name)
            else:
                insert_record(threats, record, name)
                stats["failed"] += record.count

        stats["paligned"] = (
            stats["total"] - (stats["aligned"] + stats["failed"]))
        pie_data = {}
        if stats["total"]:
            pie_data.update({
                "faligned": stats["aligned"] / float(stats["total"]) * 100,
                "paligned": (
                    (stats["trusted"] - stats["aligned"]) /
                    float(stats["total"]) * 100
                ),
                "failed": stats["failed"] / float(stats["total"]) * 100
            })

        context.update({
            "stats": stats, "trusted": trusted, "threats": threats,
            "period": self.period, "daterange": self.daterange,
            "domain": self.domain, "pie_data": pie_data
        })
        return context
