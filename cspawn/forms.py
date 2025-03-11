from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, DateTimeField, SelectField
from wtforms.validators import DataRequired, Optional
from cspawn.util.names import class_code


class ClassForm(FlaskForm):
    name = StringField('Class Name', validators=[DataRequired()])
    description = TextAreaField('Description', validators=[Optional()])
    location = StringField('Location', validators=[Optional()])
    term = StringField('Term', validators=[Optional()])
    class_code = StringField('Class Code', default=class_code, validators=[DataRequired()])
    start_date = DateTimeField('Start Date', format='%Y-%m-%dT%H:%M', validators=[Optional()])
    end_date = DateTimeField('End Date', format='%Y-%m-%dT%H:%M', validators=[Optional()])
    image_id = SelectField('Image', coerce=int, validators=[DataRequired()])

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
