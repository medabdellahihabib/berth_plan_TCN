from django.contrib import admin

from .models import AgentProfile, Bollard, PriorityDay, QuayConfig, Vessel, VesselCall
from .scheduler import recompute_plan


class BollardInline(admin.TabularInline):
    model = Bollard
    extra = 1
    fields = ("name", "position_m")


@admin.register(QuayConfig)
class QuayConfigAdmin(admin.ModelAdmin):
    list_display = ("name", "length_m", "start_m", "max_draft_m", "bollard_spacing_m", "nb_bollards")
    fieldsets = (
        ("Quai", {"fields": ("name", "length_m", "start_m", "max_draft_m")}),
        ("Bollards", {"fields": ("bollard_spacing_m", "bollard_prefix"),
                      "description": "Modifiez les bollards un par un ci-dessous, ou utilisez "
                                     "l'action « Régénérer les bollards » depuis la liste."}),
    )
    inlines = [BollardInline]
    actions = ["regenerer_bollards"]

    @admin.display(description="Bollards")
    def nb_bollards(self, obj):
        return obj.bollards.count()

    def has_add_permission(self, request):
        return not QuayConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Régénérer les bollards selon l'espacement")
    def regenerer_bollards(self, request, queryset):
        for quay in queryset:
            n = quay.generate_bollards()
            self.message_user(request, f"{n} bollards générés pour {quay.name}.")


@admin.register(AgentProfile)
class AgentProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "line", "company", "phone")
    list_filter = ("role", "line")
    search_fields = ("user__username", "company")


@admin.register(Vessel)
class VesselAdmin(admin.ModelAdmin):
    list_display = ("name", "imo", "loa", "line")
    list_filter = ("line",)
    search_fields = ("name", "imo")


@admin.register(VesselCall)
class VesselCallAdmin(admin.ModelAdmin):
    list_display = ("vessel", "line", "draft", "berth_from", "eta", "etb", "etd", "actual_berthing",
                    "actual_departure", "planned_start", "planned_end", "status", "is_priority")
    list_filter = ("status", "line", "is_priority")
    search_fields = ("vessel__name", "vessel__imo", "voyage")
    date_hierarchy = "eta"
    actions = ["recalculer"]

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        recompute_plan()

    @admin.action(description="Recalculer le berth plan")
    def recalculer(self, request, queryset):
        recompute_plan()
        self.message_user(request, "Berth plan recalculé.")


@admin.register(PriorityDay)
class PriorityDayAdmin(admin.ModelAdmin):
    list_display = ("weekday", "line")
    list_editable = ("line",)

    def has_add_permission(self, request):
        return PriorityDay.objects.count() < 7

    def has_delete_permission(self, request, obj=None):
        return False