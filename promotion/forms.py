from django import forms
from django.core.exceptions import ValidationError
from .models import User, Receipt

MIN_PASSWORD_LENGTH = 8


class EmailVerificationForm(forms.Form):
    code = forms.CharField(
        label='Код из письма',
        min_length=6,
        max_length=6,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '000000',
            'inputmode': 'numeric',
            'autocomplete': 'one-time-code',
        }),
    )

    def clean_code(self):
        code = self.cleaned_data['code'].strip()
        if not code.isdigit():
            raise ValidationError('Код должен состоять из 6 цифр')
        return code


class ForgotPasswordForm(forms.Form):
    email = forms.EmailField(
        label='Email',
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'your@email.com',
        }),
    )

    def clean_email(self):
        return self.cleaned_data['email'].lower()


class ResetPasswordForm(forms.Form):
    code = forms.CharField(
        label='Код из письма',
        min_length=6,
        max_length=6,
        widget=forms.TextInput(attrs={
            'class': 'form-control',
            'placeholder': '000000',
            'inputmode': 'numeric',
            'autocomplete': 'one-time-code',
        }),
    )
    password = forms.CharField(
        label='Новый пароль',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': '********',
        }),
    )
    password2 = forms.CharField(
        label='Подтверждение пароля',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': '********',
        }),
    )

    def clean_code(self):
        code = self.cleaned_data['code'].strip()
        if not code.isdigit():
            raise ValidationError('Код должен состоять из 6 цифр')
        return code

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        password2 = cleaned_data.get('password2')

        if password and len(password) < MIN_PASSWORD_LENGTH:
            self.add_error('password', f'Пароль должен содержать минимум {MIN_PASSWORD_LENGTH} символов')

        if password and password2 and password != password2:
            self.add_error('password2', 'Пароли не совпадают')

        return cleaned_data


class ParticipantRegistrationForm(forms.Form):
    email = forms.EmailField(
        label='Email',
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'your@email.com'
        })
    )

    password = forms.CharField(
        label='Пароль',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': '********'
        })
    )

    password2 = forms.CharField(
        label='Подтверждение пароля',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': '********'
        })
    )

    agree_policy = forms.BooleanField(
        widget=forms.CheckboxInput(attrs={
            'class': 'custom-checkbox__input',
        })
    )

    def clean_email(self):
        email = self.cleaned_data['email'].lower()
        existing = User.objects.filter(email=email).first()
        if existing and existing.is_active:
            raise ValidationError('Пользователь с таким email уже существует')
        return email

    def clean(self):
        cleaned_data = super().clean()
        password = cleaned_data.get('password')
        password2 = cleaned_data.get('password2')

        if password and len(password) < MIN_PASSWORD_LENGTH:
            self.add_error('password', f'Пароль должен содержать минимум {MIN_PASSWORD_LENGTH} символов')

        if password and password2 and password != password2:
            self.add_error('password2', 'Пароли не совпадают')

        return cleaned_data


class ParticipantLoginForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            'class': 'form-control',
            'placeholder': 'your@email.com'
        })
    )

    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': '********'
        })
    )


class ChangeProfilePassword(forms.Form):
    old_password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': '********'
        })
    )

    new_password_1 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': '********'
        })
    )

    new_password_2 = forms.CharField(
        widget=forms.PasswordInput(attrs={
            'class': 'form-control',
            'placeholder': '********'
        })
    )

    def __init__(self, *args, **kwargs):
        self.participant = kwargs.pop('participant', None)
        super().__init__(*args, **kwargs)

    def clean_old_password(self):
        old_password = self.cleaned_data.get('old_password')
        if self.participant is None:
            raise ValidationError('Не удалось определить пользователя')
        if not self.participant.check_password(old_password):
            raise ValidationError('Текущий пароль указан неверно')
        return old_password

    def clean(self):
        cleaned_data = super().clean()
        new_password_1 = cleaned_data.get('new_password_1')
        new_password_2 = cleaned_data.get('new_password_2')

        if new_password_1 and len(new_password_1) < MIN_PASSWORD_LENGTH:
            self.add_error('new_password_1', f'Пароль должен содержать минимум {MIN_PASSWORD_LENGTH} символов')

        if new_password_1 and new_password_2 and new_password_1 != new_password_2:
            self.add_error('new_password_2', 'Новые пароли не совпадают')

        if self.participant and new_password_1 and self.participant.check_password(new_password_1):
            self.add_error('new_password_1', 'Новый пароль должен отличаться от текущего')

        return cleaned_data


class ParticipantProfileForm(forms.ModelForm):

    birth_date = forms.DateField(
        required=True,
        input_formats=['%d-%m-%Y'],
        widget=forms.DateInput(attrs={
            'class': 'form-control datepicker',
            'type': 'text',
            'placeholder': 'ДД-ММ-ГГГГ'
        })
    )

    bank = forms.CharField(
        required=True,
        max_length=500,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Введите название банка', 'required': True, 'autocomplete': 'off'}),
    )

    bank_bik = forms.CharField(
        required=True,
        max_length=20,
        widget=forms.HiddenInput(),
    )

    middle_name = forms.CharField(
        required=False,
        max_length=150,
        widget=forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ваше отчество (при наличии)', 'autocomplete': 'off'}),
    )

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'middle_name', 'phone', 'birth_date', 'city', 'email', 'bank', 'bank_bik']
        widgets = {
            'first_name': forms.TextInput(attrs={'class': 'form-control first_name', 'placeholder': 'Ваше имя', 'required': True}),
            'last_name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ваша фамилия', 'required': True}),
            'phone': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '+7 (___) ___-__-__', 'required': True, 'type': 'tel'}),
            'email': forms.EmailInput(attrs={'class': 'form-control readonly', 'placeholder': 'pochta@test.com', 'required': True, 'readonly': True}),
            'city': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Город проживания', 'required': True, 'autocomplete': 'off'}),
        }

    def clean_bank_bik(self):
        bank_bik = self.cleaned_data.get('bank_bik', '').strip()
        if not bank_bik.isdigit() or len(bank_bik) != 9:
            raise forms.ValidationError('Выберите банк из списка подсказок')
        return bank_bik


class ManualChecksForm(forms.ModelForm):
    class Meta:
        model = Receipt
        fields = ['fn', 'fd', 'fp', 'amount', 'date']
        widgets = {
            'date': forms.DateTimeInput(attrs={
                'type': 'datetime-local',
                'class': 'form-control',
                'required': 'required'
            }),
            'fn': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Введите ФН',
                'required': 'required'
            }),
            'fd': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Введите номер чека (ФД)',
                'required': 'required'
            }),
            'fp': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': 'Введите ФП',
                'required': 'required'
            }),
            'amount': forms.NumberInput(attrs={
                'class': 'form-control',
                'placeholder': '0.00',
                'step': '0.01',
                'required': 'required'
            }),
        }
