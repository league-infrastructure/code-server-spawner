from flask_login import current_user
from datetime import datetime
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, DateTimeField, SelectField, BooleanField
from wtforms.validators import DataRequired, Optional
from cspawn.models import HostImage
from cspawn.util.names import class_code
from wtforms import validators
import pytz


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

    @classmethod
    def from_model(cls, model):
        form = cls()
        for field in form._fields:
            if hasattr(model, field):
                form._fields[field].data = getattr(model, field)
        return form

    def to_model(self, model):
        for field in self._fields:
            if hasattr(model, field):
                setattr(model, field, self._fields[field].data)

        if not self.name.data or not self.description.data:
            image = HostImage.query.get(self.image_id.data)
            model.name = model.name or image.name
            model.description = model.description or image.desc

        if not model.timezone:
            model.timezone = current_user.timezone

        if not model.start_date:
            model.start_date = datetime.now(pytz.timezone(model.timezone))

        if model.end_date:
            model.end_date = model.end_date.astimezone(model.timezone)
