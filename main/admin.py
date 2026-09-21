from django.contrib import admin

from .models import (
    FAQ,
    ActionProducts,
    Address,
    City,
    CompanyInfo,
    ContentBlock,
    MomentPrizes,
    MonthPrizes,
    ParticipationStep,
    SEOSettings,
    SpecialPrizes,
    WeekPrizes,
)

admin.site.register(CompanyInfo)
admin.site.register(SEOSettings)


@admin.register(ContentBlock)
class ContentBlockAdmin(admin.ModelAdmin):
    list_display = ('code', 'title', 'is_visible', 'order')
    list_editable = ('is_visible', 'order')
    list_filter = ('is_visible',)


@admin.register(ParticipationStep)
class ParticipationStepAdmin(admin.ModelAdmin):
    list_display = ('number', 'title')
    list_editable = ('title',)


@admin.register(ActionProducts)
class ActionProductsAdmin(admin.ModelAdmin):
    list_display = ('name', 'show_in_products_block', 'products_block_order')
    list_editable = ('show_in_products_block', 'products_block_order')
    list_filter = ('show_in_products_block',)


admin.site.register(MomentPrizes)
admin.site.register(WeekPrizes)
admin.site.register(MonthPrizes)
admin.site.register(SpecialPrizes)
admin.site.register(FAQ)


class AddressInline(admin.TabularInline):
    model = Address
    extra = 1


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    inlines = (AddressInline,)
