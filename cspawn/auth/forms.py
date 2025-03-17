from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, EqualTo, ValidationError
from cspawn.models import User, Class
from wtforms import Form


class UPRegistrationForm(FlaskForm):
    class_code = StringField("Class Code", validators=[DataRequired()])
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    confirm_password = PasswordField(
        "Confirm Password", validators=[DataRequired(), EqualTo("password", message="Passwords must match")]
    )

    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user:
            raise ValidationError("Username is taken")

    def validate_class_code(self, class_code):
        class_ = Class.query.filter_by(class_code=class_code.data.strip()).first()
        if not class_:
            raise ValidationError("Invalid class code")

    def validate_password(self, password):
        if len(password.data) < 8:
            raise ValidationError("Password must be at least 8 characters long")


class GoogleRegistrationForm(FlaskForm):
    class_code = StringField("Class Code", validators=[DataRequired()])

    def validate_class_code(self, class_code):
        class_ = Class.query.filter_by(class_code=class_code.data.strip()).first()
        if not class_:
            raise ValidationError("Invalid class code")


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = StringField("Password / Class Code ", validators=[DataRequired()])
    submit = SubmitField("Sign In")

    def validate_username(self, username):
        user = User.query.filter_by(username=username.data).first()
        if user is None:
            raise ValidationError("Invalid username or password")

    def validate_password(self, password):
        user = User.query.filter_by(username=self.username.data).first()
        if user and user.password != password.data:
            raise ValidationError("Invalid username or password")
