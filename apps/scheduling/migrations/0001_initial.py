"""
Office hours and appointments.

Hand-edited in one respect: ``BtreeGistExtension`` is the first operation. The two
``ExclusionConstraint``s on Booking are GiST indexes over a range column *and* a
plain equality column, and Postgres cannot build that index without btree_gist —
so without this the migration fails, and with it the whole double-booking defence
exists. It is listed before every CreateModel because operations run in order.

Creating an extension requires a role with sufficient privilege. In the compose
stack the application role owns its own database and this succeeds; on a managed
Postgres it may need an operator to run ``CREATE EXTENSION btree_gist`` once
first, after which this operation is a no-op.

The three Google columns on Booking are here from the start although nothing
writes them until the calendar sync lands. Shipping them now means that becomes a
code change rather than a schema change on a live database.
"""

import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('counseling', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        BtreeGistExtension(),
        migrations.CreateModel(
            name='AvailabilityOverride',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('date', models.DateField()),
                ('is_available', models.BooleanField(default=False, help_text='Unticked closes the time; ticked opens it.')),
                ('start_time', models.TimeField(blank=True, null=True)),
                ('end_time', models.TimeField(blank=True, null=True)),
                ('reason', models.CharField(blank=True, max_length=120)),
                ('counselor', models.ForeignKey(limit_choices_to={'is_active': True, 'role': 'counselor'}, on_delete=django.db.models.deletion.CASCADE, related_name='availability_overrides', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['date', 'start_time'],
                'indexes': [models.Index(fields=['counselor', 'date'], name='scheduling__counsel_de61c3_idx')],
                'constraints': [models.CheckConstraint(condition=models.Q(models.Q(('end_time__isnull', True), ('start_time__isnull', True)), models.Q(('end_time__isnull', False), ('start_time__isnull', False)), _connector='OR'), name='override_has_both_times_or_neither'), models.CheckConstraint(condition=models.Q(('start_time__isnull', True), ('end_time__gt', models.F('start_time')), _connector='OR'), name='override_window_ends_after_it_starts'), models.CheckConstraint(condition=models.Q(('is_available', False), ('start_time__isnull', False), _connector='OR'), name='extra_availability_needs_a_window'), models.UniqueConstraint(condition=models.Q(('start_time__isnull', True)), fields=('counselor', 'date'), name='uniq_all_day_override_per_date')],
            },
        ),
        migrations.CreateModel(
            name='AvailabilityRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('weekday', models.IntegerField(choices=[(0, 'Monday'), (1, 'Tuesday'), (2, 'Wednesday'), (3, 'Thursday'), (4, 'Friday'), (5, 'Saturday'), (6, 'Sunday')])),
                ('start_time', models.TimeField(help_text='In your own timezone.')),
                ('end_time', models.TimeField()),
                ('slot_minutes', models.PositiveSmallIntegerField(default=60, help_text='How long each appointment in this window is.')),
                ('effective_from', models.DateField(default=django.utils.timezone.localdate)),
                ('effective_to', models.DateField(blank=True, help_text='Leave blank while these hours are open-ended.', null=True)),
                ('counselor', models.ForeignKey(limit_choices_to={'is_active': True, 'role': 'counselor'}, on_delete=django.db.models.deletion.CASCADE, related_name='availability_rules', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['weekday', 'start_time'],
                'indexes': [models.Index(fields=['counselor', 'weekday'], name='scheduling__counsel_8e28e1_idx')],
                'constraints': [models.CheckConstraint(condition=models.Q(('end_time__gt', models.F('start_time'))), name='availability_window_ends_after_it_starts'), models.CheckConstraint(condition=models.Q(('slot_minutes__gte', 15), ('slot_minutes__lte', 480)), name='availability_slot_length_is_plausible'), models.CheckConstraint(condition=models.Q(('effective_to__isnull', True), ('effective_to__gte', models.F('effective_from')), _connector='OR'), name='availability_effective_range_is_ordered'), models.UniqueConstraint(fields=('counselor', 'weekday', 'start_time', 'end_time', 'effective_from'), name='uniq_availability_window')],
            },
        ),
        migrations.CreateModel(
            name='Booking',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True, db_index=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('attendance', models.CharField(choices=[('individual', 'Just me and my counselor'), ('whole_case', 'Everyone on the case')], default='individual', max_length=20)),
                ('slot', django.contrib.postgres.fields.ranges.DateTimeRangeField()),
                ('status', models.CharField(choices=[('requested', 'Requested'), ('confirmed', 'Confirmed'), ('cancelled', 'Cancelled'), ('completed', 'Completed'), ('no_show', 'Did not attend')], db_index=True, default='requested', max_length=20)),
                ('request_note', models.TextField(blank=True, help_text='Anything your counselor should know.')),
                ('counselor_note', models.TextField(blank=True)),
                ('cancelled_at', models.DateTimeField(blank=True, null=True)),
                ('cancellation_reason', models.CharField(blank=True, max_length=200)),
                ('was_late_cancellation', models.BooleanField(default=False)),
                ('reminder_sent_at', models.DateTimeField(blank=True, null=True)),
                ('google_event_id', models.CharField(blank=True, max_length=1024)),
                ('google_etag', models.CharField(blank=True, max_length=255)),
                ('google_synced_at', models.DateTimeField(blank=True, null=True)),
                ('cancelled_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='bookings_cancelled', to=settings.AUTH_USER_MODEL)),
                ('case', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='bookings', to='counseling.case')),
                ('counselee', models.ForeignKey(help_text='Whose appointment this is. For a joint session, whoever booked it.', limit_choices_to={'role': 'counselee'}, on_delete=django.db.models.deletion.PROTECT, related_name='bookings_as_counselee', to=settings.AUTH_USER_MODEL)),
                ('counselor', models.ForeignKey(limit_choices_to={'role': 'counselor'}, on_delete=django.db.models.deletion.PROTECT, related_name='bookings_as_counselor', to=settings.AUTH_USER_MODEL)),
                ('created_by', models.ForeignKey(help_text='Who made the booking — the counselee, the counselor, or an admin.', on_delete=django.db.models.deletion.PROTECT, related_name='bookings_created', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-slot'],
                'indexes': [models.Index(fields=['counselor', 'status'], name='scheduling__counsel_e3b590_idx'), models.Index(fields=['case', '-created_at'], name='scheduling__case_id_86d263_idx'), models.Index(fields=['counselee', 'status'], name='scheduling__counsel_ca1940_idx'), models.Index(fields=['reminder_sent_at', 'status'], name='scheduling__reminde_847c1f_idx')],
                'constraints': [django.contrib.postgres.constraints.ExclusionConstraint(condition=models.Q(('status__in', ('requested', 'confirmed'))), expressions=[('slot', '&&'), ('counselor', '=')], name='no_overlapping_bookings_per_counselor'), django.contrib.postgres.constraints.ExclusionConstraint(condition=models.Q(('status__in', ('requested', 'confirmed'))), expressions=[('slot', '&&'), ('counselee', '=')], name='no_overlapping_bookings_per_counselee'), models.CheckConstraint(condition=models.Q(('slot__isempty', False), ('slot__lower_inf', False), ('slot__upper_inf', False)), name='booking_slot_is_a_real_interval'), models.CheckConstraint(condition=models.Q(models.Q(('status', 'cancelled'), _negated=True), ('cancelled_at__isnull', False), _connector='OR'), name='cancelled_booking_records_when')],
            },
        ),
    ]
