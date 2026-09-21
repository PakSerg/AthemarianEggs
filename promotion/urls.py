from django.urls import path
from . import views

app_name = 'promotion'

urlpatterns = [
    path('login/', views.ParticipantLoginView.as_view(), name='login'),
    path('register/', views.ParticipantRegisterView.as_view(), name='register'),
    path('verify-email/', views.ParticipantVerifyEmailView.as_view(), name='verify_email'),
    path('forgot-password/', views.ParticipantForgotPasswordView.as_view(), name='forgot_password'),
    path('reset-password/', views.ParticipantResetPasswordView.as_view(), name='reset_password'),
    path('dashboard/', views.ParticipantDashboardView.as_view(), name='dashboard'),
    path('profile/', views.ParticipantProfileView.as_view(), name='profile'),
    path('prizes/<int:draw_result_id>/file/', views.ParticipantPrizeFileView.as_view(), name='prize-file'),
    path('api/upload-check-image/', views.upload_check_image, name='upload-check-image'),
    path('api/upload-check-qr/', views.upload_check_qr, name='upload-check-qr'),
    path('api/sbp-banks/', views.sbp_banks_suggest, name='sbp-banks'),
    path('api/instant/play/', views.instant_play, name='instant-play'),
    path('logout/', views.ParticipantLogoutView.as_view(), name='logout'),
    
    path('unsubscribe/<str:token>/', views.unsubscribe_receipt_emails, name='unsubscribe_receipt_emails'),

    # Для разработки
    path('delete-all-receipts/', views.delete_all_receipts, name='delete-all-receipts'),
    path('dev/email-preview/<str:template_name>/', views.email_preview, name='email-preview'),
]
