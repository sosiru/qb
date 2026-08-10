# Generated for predefined Quick Bills payment category taxonomy.

from django.db import migrations, models
from django.template.defaultfilters import slugify


PREDEFINED_CATEGORIES = [
    ("electricity", "Electricity", "Electricity providers and power bills.", "bolt", 10),
    ("water", "Water", "Water utilities and county water providers.", "droplets", 20),
    ("internet", "Internet", "Home internet, business internet, and fiber providers.", "wifi", 30),
    ("mobile_telecommunications", "Mobile / Telecommunications", "Airtime, mobile network, and telecommunications services.", "smartphone", 40),
    ("tv_entertainment", "TV / Entertainment", "Pay TV, streaming, and entertainment subscriptions.", "tv", 50),
    ("rent_property", "Rent / Property", "Rent, landlords, property managers, and property service payments.", "home", 60),
    ("insurance", "Insurance", "Health, motor, life, and general insurance payments.", "shield", 70),
    ("banking_loans", "Banking / Loans", "Bank repayments, loans, credit facilities, and financial institutions.", "landmark", 80),
    ("education", "School / Education", "School fees, colleges, universities, and education payments.", "graduation-cap", 90),
    ("government_taxes", "Government / Taxes", "Government services, taxes, licenses, and public-service payments.", "building-2", 100),
    ("security_services", "Security Services", "Home security, business security, and alarm monitoring.", "lock", 110),
    ("waste_management", "Waste Management", "Garbage collection, disposal, and environmental services.", "trash-2", 120),
    ("healthcare", "Healthcare", "Hospitals, clinics, medical facilities, and medical subscriptions.", "heart-pulse", 130),
    ("transport_parking", "Transport / Parking", "Parking, transport services, and fleet-related recurring payments.", "car", 140),
    ("professional_business_services", "Professional / Business Services", "Accounting, legal, SaaS, and professional subscriptions.", "briefcase-business", 150),
]


def populate_categories(apps, schema_editor):
    ExpenseCategory = apps.get_model("base", "ExpenseCategory")

    used_slugs = set()
    for category in ExpenseCategory.objects.all().order_by("created_at"):
        slug = category.slug or slugify(category.name).replace("-", "_") or "category"
        original = slug
        suffix = 2
        while slug in used_slugs or ExpenseCategory.objects.exclude(pk=category.pk).filter(slug=slug).exists():
            slug = f"{original}_{suffix}"
            suffix += 1
        used_slugs.add(slug)
        ExpenseCategory.objects.filter(pk=category.pk).update(slug=slug)

    for slug, name, description, icon, display_order in PREDEFINED_CATEGORIES:
        ExpenseCategory.objects.update_or_create(
            slug=slug,
            defaults={
                "name": name,
                "description": description,
                "category_type": "PAYMENT",
                "icon": icon,
                "display_order": display_order,
                "active": True,
                "is_system_defined": True,
            },
        )


class Migration(migrations.Migration):

    dependencies = [
        ("base", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="expensecategory",
            name="slug",
            field=models.SlugField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="expensecategory",
            name="category_type",
            field=models.CharField(default="PAYMENT", max_length=32),
        ),
        migrations.AddField(
            model_name="expensecategory",
            name="icon",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddField(
            model_name="expensecategory",
            name="display_order",
            field=models.PositiveIntegerField(default=1000),
        ),
        migrations.AddField(
            model_name="expensecategory",
            name="is_system_defined",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(populate_categories, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="expensecategory",
            name="slug",
            field=models.SlugField(blank=True, max_length=64, unique=True),
        ),
        migrations.AlterModelOptions(
            name="expensecategory",
            options={"ordering": ["display_order", "name", "created_at"]},
        ),
    ]
