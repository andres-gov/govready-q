# Generated by Django 2.0.13 on 2019-08-01 20:00

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('siteapp', '0028_auto_20190717_2006'),
    ]

    operations = [
        migrations.AlterField(
            model_name='portfolio',
            name='title',
            field=models.CharField(help_text='The title of this Portfolio.', max_length=256, unique=True),
        ),
        migrations.AlterField(
            model_name='project',
            name='root_task',
            field=models.ForeignKey(blank=True, help_text="All Projects have a 'root Task' (e.g., 'guidedmodules.task'). The root Task defines important information about Project.", null=True, on_delete=django.db.models.deletion.CASCADE, related_name='root_of', to='guidedmodules.Task'),
        ),
    ]
