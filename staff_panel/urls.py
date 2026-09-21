from django.urls import path

from . import views
from . import views_cyclops

app_name = 'panel'

urlpatterns = [
    path('', views.IndexRedirectView.as_view(), name='index'),
    path('login/', views.LoginView.as_view(), name='login'),
    path('logout/', views.LogoutView.as_view(), name='logout'),

    path('analytics/', views.AnalyticsView.as_view(), name='analytics'),
    path('analytics/export/', views.AnalyticsExportView.as_view(), name='analytics_export'),

    # --- Чеки ---
    path('receipts/', views.ReceiptListView.as_view(), name='receipts'),
    path('receipts/export/', views.ReceiptExportView.as_view(), name='receipts_export'),
    path('receipts/<int:pk>/', views.ReceiptDetailView.as_view(), name='receipt_detail'),
    path('receipts/<int:pk>/update/', views.ReceiptUpdateView.as_view(), name='receipt_update'),
    path('receipts/<int:pk>/set-status/<str:status>/', views.ReceiptSetStatusView.as_view(), name='receipt_set_status'),

    # --- Моментальные призы ---
    path('instant-prizes/', views.InstantPrizesView.as_view(), name='instant_prizes'),
    path('instant-prizes/generate/', views.InstantPrizesGenerateView.as_view(), name='instant_prizes_generate'),

    # --- Призы ---
    path('prizes/', views.PrizeListView.as_view(), name='prizes'),
    path('prizes/export/', views.PrizeExportView.as_view(), name='prizes_export'),
    path('prizes/<int:pk>/', views.PrizeDetailView.as_view(), name='prize_detail'),
    path('prizes/<int:pk>/update/', views.PrizeUpdateView.as_view(), name='prize_update'),

    # --- Участники ---
    path('participants/', views.ParticipantListView.as_view(), name='participants'),
    path('participants/export/', views.ParticipantExportView.as_view(), name='participants_export'),
    path('participants/<int:pk>/', views.ParticipantDetailView.as_view(), name='participant_detail'),
    path('participants/<int:pk>/update/', views.ParticipantUpdateView.as_view(), name='participant_update'),

    # --- Победители ---
    path('winners/', views.WinnerListView.as_view(), name='winners'),
    path('winners/export/', views.WinnerExportView.as_view(), name='winners_export'),
    path('winners/create/', views.WinnerCreateView.as_view(), name='winner_create'),
    path('winners/participant-search/', views.WinnerParticipantSearchView.as_view(), name='winner_participant_search'),
    path(
        'winners/receipts-for-participant/',
        views.WinnerReceiptsForParticipantView.as_view(),
        name='winner_receipts_for_participant',
    ),
    path('winners/auto-distribute/', views.WinnerAutoDistributeView.as_view(), name='winner_auto_distribute'),
    path(
        'winners/replacement-settings/',
        views.WinnerReplacementSettingsView.as_view(),
        name='winner_replacement_settings',
    ),
    path('winners/<str:kind>/<int:pk>/update-field/', views.WinnerUpdateFieldView.as_view(), name='winner_update_field'),
    path('winners/<str:kind>/<int:pk>/prize-file-options/', views.WinnerPrizeFileOptionsView.as_view(), name='winner_prize_file_options'),
    path('winners/<str:kind>/<int:pk>/replace/', views.WinnerReplaceView.as_view(), name='winner_replace'),
    path(
        'winners/<str:kind>/<int:pk>/replace/candidates/',
        views.WinnerReplaceCandidatesView.as_view(),
        name='winner_replace_candidates',
    ),
    path('winners/weekly/<int:pk>/publish/', views.WinnerPublishOneView.as_view(), name='winner_publish_one'),
    path('winners/weekly/<int:pk>/send-shipping-soon/', views.WinnerSendShippingSoonView.as_view(), name='winner_send_shipping_soon'),
    path('winners/<str:kind>/<int:pk>/', views.WinnerDetailView.as_view(), name='winner_detail'),
    path('winners/<str:kind>/<int:pk>/issue-oki/', views.WinnerIssueOkiView.as_view(), name='winner_issue_oki'),
    path('winners/<str:kind>/<int:pk>/send-email/', views.WinnerSendEmailView.as_view(), name='winner_send_email'),
    path('winners/<str:kind>/<int:pk>/delete/', views.WinnerDeleteView.as_view(), name='winner_delete'),

    # --- Файлы электронных призов ---
    path('prize-files/', views.PrizeFileListView.as_view(), name='prize_files'),
    path('prize-files/upload/', views.PrizeFileUploadView.as_view(), name='prize_file_upload'),
    path('prize-files/<int:pk>/download/', views.PrizeFileDownloadView.as_view(), name='prize_file_download'),
    path('prize-files/<int:pk>/delete/', views.PrizeFileDeleteView.as_view(), name='prize_file_delete'),

    # --- Cyclops (Точка Банк) ---
    path('cyclops/', views_cyclops.CyclopsView.as_view(), name='cyclops'),
    path('cyclops/sync-sbp-banks/', views_cyclops.CyclopsSyncSbpBanksView.as_view(), name='cyclops_sync_sbp_banks'),
    path('cyclops/sync-payments/', views_cyclops.CyclopsSyncPaymentsView.as_view(), name='cyclops_sync_payments'),
    path('cyclops/sync-beneficiaries/', views_cyclops.CyclopsSyncBeneficiariesView.as_view(), name='cyclops_sync_beneficiaries'),
    path('cyclops/beneficiary/create/', views_cyclops.CyclopsBeneficiaryCreateView.as_view(), name='cyclops_beneficiary_create'),
    path('cyclops/beneficiary/<int:pk>/toggle-active/', views_cyclops.CyclopsBeneficiaryToggleActiveView.as_view(), name='cyclops_beneficiary_toggle_active'),
    path('cyclops/beneficiary/<int:pk>/upload-document/', views_cyclops.CyclopsBeneficiaryUploadDocumentView.as_view(), name='cyclops_beneficiary_upload_document'),
    path('cyclops/payment/identify/', views_cyclops.CyclopsIdentifyPaymentView.as_view(), name='cyclops_identify_payment'),
    path('cyclops/payment/test/', views_cyclops.CyclopsTestPaymentView.as_view(), name='cyclops_test_payment'),
    path('cyclops/payout/test/', views_cyclops.CyclopsTestParticipantPayoutView.as_view(), name='cyclops_test_participant_payout'),
    path('cyclops/payout/<int:pk>/trigger/', views_cyclops.CyclopsPayoutTriggerView.as_view(), name='cyclops_payout_trigger'),
    path('cyclops/payouts/export/', views_cyclops.CyclopsPayoutExportView.as_view(), name='cyclops_payouts_export'),
    path('cyclops/settings/', views_cyclops.CyclopsSettingsView.as_view(), name='cyclops_settings'),
    path('cyclops/service-agreement/preview/', views_cyclops.CyclopsServiceAgreementPreviewView.as_view(), name='cyclops_service_agreement_preview'),
]
