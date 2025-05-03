from flask_login import current_user
from datetime import datetime
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, DateTimeField, SelectField, BooleanField
from wtforms.validators import DataRequired, Optional, ValidationError
from cspawn.models import ClassProto, Class, User
from cspawn.util.names import class_code
from wtforms import validators
import pytz
from zoneinfo import ZoneInfo


class ConditionalDataRequired(validators.DataRequired):
    def __init__(self, other_field_name, *args, **kwargs):
        self.other_field_name = other_field_name
        super().__init__(*args, **kwargs)

    def __call__(self, form, field):
        other_field = form._fields.get(self.other_field_name)
        if other_field is not None and not other_field.data:
            super().__call__(form, field)


class ClassForm(FlaskForm):
    name = StringField("Class Name", validators=[Optional()])
    description = TextAreaField("Description", validators=[Optional()])
    location = StringField("Location", validators=[Optional()])
    term = StringField("Term", validators=[Optional()])
    class_code = StringField("Class Code", default=class_code, validators=[DataRequired()])
    start_date = DateTimeField("Start Date", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    end_date = DateTimeField("End Date", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    image_id = SelectField("Image", coerce=int, validators=[Optional()])

    active = BooleanField("Active", default=True, validators=[Optional()])
    hidden = BooleanField("Hidden", default=False, validators=[Optional()])
    public = BooleanField("Public", default=True, validators=[Optional()])

    def validate_end_date(self, field):
        if self.start_date.data and field.data and self.start_date.data >= field.data:
            raise ValidationError("End date must be after start date")

    @classmethod
    def from_model(cls, model: Class):
        form = cls()
        for field in form._fields:
            if hasattr(model, field):
                form._fields[field].data = getattr(model, field)

        if form.start_date.data:
            form.start_date.data = form.start_date.data.astimezone(ZoneInfo(model.timezone))
        if form.end_date.data:
            form.end_date.data = form.end_date.data.astimezone(ZoneInfo(model.timezone))

        return form

    def to_model(self, model: Class, user: User):
        for field in self._fields:
            if hasattr(model, field):
                setattr(model, field, self._fields[field].data)

        if not self.name.data or not self.description.data:
            image = ClassProto.query.get(self.image_id.data)
            model.name = model.name or image.name
            model.description = model.description or image.desc

        if not model.timezone:
            model.timezone = user.timezone

        tz = ZoneInfo(model.timezone)

        if not model.start_date:
            model.start_date = datetime.now(tz)

        if model.start_date and model.start_date.tzinfo is None:
            model.start_date = model.start_date.replace(tzinfo=tz)

        if model.end_date and model.end_date.tzinfo is None:
            model.end_date = model.end_date.replace(tzinfo=tz)
