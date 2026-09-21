from django import forms

from promotion.models import PromotionDrawResult, PromotionDrawResultMainRaffle, Prize, Receipt, User


class ReceiptUpdateForm(forms.ModelForm):
    class Meta:
        model = Receipt
        fields = ('status', 'message')
        widgets = {
            'message': forms.Textarea(attrs={'rows': 4}),
        }


class ParticipantUpdateForm(forms.ModelForm):
    birth_date = forms.DateField(
        required=False,
        input_formats=['%d-%m-%Y'],
        widget=forms.DateInput(attrs={
            'class': 'panel-field__input datepicker',
            'type': 'text',
            'placeholder': 'ДД-ММ-ГГГГ',
        }),
    )

    class Meta:
        model = User
        fields = (
            'first_name', 'last_name', 'middle_name', 'phone', 'city', 'address',
            'birth_date', 'is_active', 'is_blocked', 'guaranteed_prize_opt_out',
        )


class DrawResultCreateForm(forms.ModelForm):
    """
    Каскадная логика: чек доступен только после выбора победителя и принадлежит
    только ему, приз нельзя выбрать повторно, если уже занят в другом розыгрыше.
    """

    class Meta:
        model = PromotionDrawResult
        fields = ('participant', 'prize', 'receipt', 'week_num', 'month_num')
        widgets = {
            'participant': forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        participant_id = self._resolve_participant_id()
        receipt_field = self.fields['receipt']
        receipt_field.required = False

        prize_field = self.fields['prize']
        used_prize_ids = set(
            PromotionDrawResult.objects.filter(is_reserve=False).values_list('prize_id', flat=True)
        ) | set(
            PromotionDrawResultMainRaffle.objects.filter(is_reserve=False).values_list('prize_id', flat=True),
        )
        used_prize_ids.discard(self.instance.prize_id)
        prize_field.queryset = Prize.objects.exclude(pk__in=used_prize_ids)

        if participant_id:
            receipt_field.queryset = (
                Receipt.objects.filter(participant_id=participant_id)
                .select_related('participant')
                .order_by('-created_at')
            )
        else:
            receipt_field.queryset = Receipt.objects.none()

    def _resolve_participant_id(self):
        if self.instance.pk and self.instance.participant_id:
            return self.instance.participant_id
        if self.data:
            raw = self.data.get('participant')
            if raw:
                try:
                    return int(raw)
                except (TypeError, ValueError):
                    return None
        participant = self.initial.get('participant')
        if participant is not None:
            return participant.pk if hasattr(participant, 'pk') else int(participant)
        return None

    def clean(self):
        cleaned_data = super().clean()
        participant = cleaned_data.get('participant')
        receipt = cleaned_data.get('receipt')
        if participant and receipt and receipt.participant_id != participant.id:
            self.add_error('receipt', 'Чек должен принадлежать выбранному победителю.')
        return cleaned_data


class PrizeUpdateForm(forms.ModelForm):
    """
    is_main/is_active явно объявлены как BooleanField: у Prize они nullable
    (null=True), из-за чего Django ModelForm подставляет NullBooleanField, который
    не понимает значение "on" от обычного чекбокса и молча обнуляет поле.
    """
    is_main = forms.BooleanField(required=False, label='Главный приз')
    is_active = forms.BooleanField(required=False, label='Активен')
    is_electronic = forms.BooleanField(required=False, label='Электронный приз')

    class Meta:
        model = Prize
        fields = (
            'name', 'description', 'image', 'type_prize',
            'draw_period', 'week', 'month',
            'count', 'cost', 'ndfl', 'is_main', 'is_active', 'is_electronic',
        )
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
        }

    def clean(self):
        cleaned_data = super().clean()
        is_main = cleaned_data.get('is_main')
        draw_period = cleaned_data.get('draw_period')

        if is_main:
            cleaned_data['week'] = None
            cleaned_data['month'] = None
            return cleaned_data

        if draw_period == Prize.DrawPeriod.WEEKLY:
            if not cleaned_data.get('week'):
                self.add_error('week', 'Укажите номер недели для еженедельного приза.')
            cleaned_data['month'] = None
        elif draw_period == Prize.DrawPeriod.MONTHLY:
            if not cleaned_data.get('month'):
                self.add_error('month', 'Укажите номер месяца для ежемесячного приза.')
            cleaned_data['week'] = None

        return cleaned_data
