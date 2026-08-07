# pharmacist/urls.py - COMPLETE FIXED VERSION
# ═════════════════════════════════════════════════════════════════════════════
# 
# ✅ FIXES APPLIED:
#    • Removed duplicate urlpatterns definitions
#    • Removed duplicate app_name declarations
#    • Added @csrf_exempt to dispatcher function for POST requests
#    • Organized all endpoints in logical groups
#    • Added proper comments for each endpoint group
#    • Fixed routing for Return to Provider functionality
#
# ═════════════════════════════════════════════════════════════════════════════

from django.urls import path
from django.views.decorators.csrf import csrf_exempt
from rest_framework.response import Response

from .views import (
    # Dashboard
    DashboardSummaryView,

    # Patient search (for pharmacist to look up registered patients)
    PatientSearchView,

    # Medicines
    MedicineListView,
    MedicineDetailView,
    MedicineCreateView,
    MedicineUpdateView,
    MedicineSearchView,

    # Batches
    BatchListView,
    BatchDetailView,
    BatchCreateView,
    BatchUpdateView,
    BatchReturnToProviderView,
    ExpiringBatchesListView,
    DepletedBatchesAuditView,

    # Stock
    StockAlertListView,
    StockAlertResolveView,
    StockLogListView,

    # Bills — list + create
    BillListView,
    CreateBillView,

    # Bills — detail + actions
    BillDetailView,
    AddMedicineItemView,
    AddProcedureItemView,
    RemoveMedicineItemView,
    RemoveProcedureItemView,
    TransitionBillToOpenView,
    CompleteBillView,
    MarkBillPaidView,
    SendBillToReceptionView,
    SetBillDiscountView,
    ReopenBillView,
    CancelBillView,
    confirm_dispensing_view,  # ← NEW

    # Prescription medicines
    FetchPrescriptionMedicinesView,
    PrescriptionQueueListView,

    # Medicine returns to provider
    MedicineReturnToProviderCreateView,
    MedicineReturnToProviderListView,
    MedicineReturnToProviderDetailView,
    MedicineReturnToProviderApproveView,
    MedicineReturnToProviderCompleteView,
    MedicinesForReturnListView,
    BatchesForReturnListView,
    BatchDetailForReturnView,

    # Supplies & consumables (separate stock system, same app)
    SupplyDashboardSummaryView,
    SupplyItemListView,
    SupplyItemCreateView,
    SupplyItemDetailView,
    SupplyItemUpdateView,
    AddStockView,
    UseStockView,
    SupplyBatchDetailView,
    ReturnToProviderView,
    SupplyReturnListView,
    UsageLogListView,
    AlertListView,
    AlertResolveView,

    # General Items (non-medicine retail products: diapers, tissues, soap, etc.)
    GeneralItemListView,
    GeneralItemDetailView,
    GeneralItemCreateView,
    GeneralItemUpdateView,
    GeneralItemSearchView,
    GeneralItemBatchListView,
    GeneralItemBatchDetailView,
    GeneralItemBatchCreateView,
    GeneralItemBatchUpdateView,
    GeneralItemStockAlertListView,
    GeneralItemStockAlertResolveView,
    AddGeneralItemView,
    RemoveGeneralItemView,
    GeneralItemReturnToProviderView,
    GeneralItemReturnListView,
)

app_name = 'pharmacist'


# ✅ Dispatcher function: routes POST and GET to appropriate views
@csrf_exempt
def medicine_return_to_provider_dispatcher(request, *args, **kwargs):
    """
    Dispatches a single URL to different APIViews based on HTTP method:
    - POST -> MedicineReturnToProviderCreateView (create a return)
    - GET  -> MedicineReturnToProviderListView   (list returns)

    This is necessary because `path()` requires one callable per route,
    and we need to support both GET and POST on the same endpoint.
    
    @csrf_exempt is added because this is a plain function, not a DRF APIView.
    DRF APIViews are automatically CSRF-exempt when using token/JWT auth,
    but plain functions need explicit exemption.
    """
    if request.method == 'POST':
        return MedicineReturnToProviderCreateView.as_view()(request, *args, **kwargs)
    elif request.method == 'GET':
        return MedicineReturnToProviderListView.as_view()(request, *args, **kwargs)
    return Response({'error': 'Method not allowed'}, status=405)


urlpatterns = [

    # ────────────────────────────────────────────────────────────────────────
    # DASHBOARD
    # ────────────────────────────────────────────────────────────────────────
    path('dashboard/', 
         DashboardSummaryView.as_view(), 
         name='dashboard'),

    # ────────────────────────────────────────────────────────────────────────
    # PATIENT SEARCH (pharmacist MRD / name lookup)
    # ────────────────────────────────────────────────────────────────────────
    # GET /api/pharmacist/patients/search/?mrd=MRD-0001
    # GET /api/pharmacist/patients/search/?q=Rahul
    # GET /api/pharmacist/patients/search/?patient_id=12
    path('patients/search/', 
         PatientSearchView.as_view(), 
         name='patient-search'),

    # ────────────────────────────────────────────────────────────────────────
    # MEDICINES
    # ────────────────────────────────────────────────────────────────────────
    path('medicines/',                 
         MedicineListView.as_view(),   
         name='medicine-list'),
    
    path('medicines/create/',          
         MedicineCreateView.as_view(), 
         name='medicine-create'),
    
    path('medicines/search/',          
         MedicineSearchView.as_view(), 
         name='medicine-search'),
    
    path('medicines/<int:pk>/',        
         MedicineDetailView.as_view(), 
         name='medicine-detail'),
    
    path('medicines/<int:pk>/update/', 
         MedicineUpdateView.as_view(), 
         name='medicine-update'),

    # ────────────────────────────────────────────────────────────────────────
    # BATCHES
    # ────────────────────────────────────────────────────────────────────────
    path('batches/',                   
         BatchListView.as_view(),   
         name='batch-list'),
    
    path('batches/create/',            
         BatchCreateView.as_view(), 
         name='batch-create'),
    
    path('batches/<int:pk>/',          
         BatchDetailView.as_view(), 
         name='batch-detail'),
    
    path('batches/<int:pk>/update/',   
         BatchUpdateView.as_view(), 
         name='batch-update'),
    
    # Batch Return to Provider
    path('batches/<int:batch_id>/return-to-provider/', 
         BatchReturnToProviderView.as_view(), 
         name='batch-return-to-provider'),
    
    path('batches/expiring/',          
         ExpiringBatchesListView.as_view(), 
         name='expiring-batches'),
    
    path('batches/depleted/',          
         DepletedBatchesAuditView.as_view(), 
         name='depleted-batches-audit'),

    # ────────────────────────────────────────────────────────────────────────
    # STOCK ALERTS
    # ────────────────────────────────────────────────────────────────────────
    path('stock-alerts/',                        
         StockAlertListView.as_view(),    
         name='stock-alert-list'),
    
    path('stock-alerts/<int:alert_id>/resolve/', 
         StockAlertResolveView.as_view(), 
         name='stock-alert-resolve'),

    # ────────────────────────────────────────────────────────────────────────
    # STOCK LOGS
    # ────────────────────────────────────────────────────────────────────────
    path('stock-logs/', 
         StockLogListView.as_view(), 
         name='stock-log-list'),

    # ────────────────────────────────────────────────────────────────────────
    # MEDICINE RETURNS TO PROVIDER
    # ────────────────────────────────────────────────────────────────────────
    # POST   /api/pharmacist/medicine-returns-to-provider/    — create return
    # GET    /api/pharmacist/medicine-returns-to-provider/    — list returns
    path('medicine-returns-to-provider/',
         medicine_return_to_provider_dispatcher,
         name='medicine-return-to-provider-list-create'),
    
    # GET /api/pharmacist/medicine-returns-to-provider/<return_id>/
    path('medicine-returns-to-provider/<int:return_id>/',
         MedicineReturnToProviderDetailView.as_view(),
         name='medicine-return-to-provider-detail'),
    
    # POST /api/pharmacist/medicine-returns-to-provider/<return_id>/approve/
    path('medicine-returns-to-provider/<int:return_id>/approve/',
         MedicineReturnToProviderApproveView.as_view(),
         name='medicine-return-to-provider-approve'),
    
    # POST /api/pharmacist/medicine-returns-to-provider/<return_id>/complete/
    path('medicine-returns-to-provider/<int:return_id>/complete/',
         MedicineReturnToProviderCompleteView.as_view(),
         name='medicine-return-to-provider-complete'),

    # ────────────────────────────────────────────────────────────────────────
    # RETURN TO PROVIDER SELECTION ENDPOINTS
    # ────────────────────────────────────────────────────────────────────────
    # GET /api/pharmacist/medicines-for-return/
    # Medicines available for return (with batches)
    path('medicines-for-return/',
         MedicinesForReturnListView.as_view(),
         name='medicines-for-return-list'),
    
    # GET /api/pharmacist/batches-for-return/
    # Batches available for return with query params: ?medicine_id=123&batch_number=B123
    path('batches-for-return/',
         BatchesForReturnListView.as_view(),
         name='batches-for-return-list'),
    
    # GET /api/pharmacist/batches/<batch_id>/for-return/
    # Get specific batch details for return
    path('batches/<int:batch_id>/for-return/',
         BatchDetailForReturnView.as_view(),
         name='batch-for-return-detail'),

    # ────────────────────────────────────────────────────────────────────────
    # BILLS: LIST + CREATE
    # ────────────────────────────────────────────────────────────────────────
    # GET  /api/pharmacist/bills/                    — list (filter: patient_type=walkin|registered)
    # POST /api/pharmacist/bills/create/             — create (all scenarios)
    path('bills/',         
         BillListView.as_view(),   
         name='bill-list'),
    
    path('bills/create/',  
         CreateBillView.as_view(), 
         name='bill-create'),

    # ────────────────────────────────────────────────────────────────────────
    # BILLS: DETAIL + LIFECYCLE ACTIONS
    # ────────────────────────────────────────────────────────────────────────
    path('bills/<int:bill_id>/',                               
         BillDetailView.as_view(),          
         name='bill-detail'),
    
    path('bills/<int:bill_id>/add-medicine/',                  
         AddMedicineItemView.as_view(),     
         name='add-medicine'),
    
    path('bills/<int:bill_id>/add-procedure/',                 
         AddProcedureItemView.as_view(),    
         name='add-procedure'),
    
    path('bills/<int:bill_id>/add-general-item/',
         AddGeneralItemView.as_view(),
         name='add-general-item'),
    
    path('bills/<int:bill_id>/transition-open/',               
         TransitionBillToOpenView.as_view(), 
         name='transition-bill-open'),
    
    path('bills/<int:bill_id>/complete/',                      
         CompleteBillView.as_view(),        
         name='complete-bill'),
    
    path('bills/<int:bill_id>/mark-paid/',                     
         MarkBillPaidView.as_view(),        
         name='mark-bill-paid'),
    
    path('bills/<int:bill_id>/send-to-reception/',
         SendBillToReceptionView.as_view(),
         name='send-bill-to-reception'),
    
    path('bills/<int:bill_id>/discount/',                      
         SetBillDiscountView.as_view(),     
         name='set-bill-discount'),
    
    path('bills/<int:bill_id>/confirm-dispensing/',             
         confirm_dispensing_view,           
         name='confirm-dispensing'),  # ← NEW
    
    path('bills/<int:bill_id>/reopen/',                        
         ReopenBillView.as_view(),          
         name='reopen-bill'),
    
    path('bills/<int:bill_id>/cancel/',                        
         CancelBillView.as_view(),          
         name='cancel-bill'),
    
    path('bills/<int:bill_id>/medicine-items/<int:item_id>/',  
         RemoveMedicineItemView.as_view(),  
         name='remove-medicine-item'),
    
    path('bills/<int:bill_id>/procedure-items/<int:item_id>/', 
         RemoveProcedureItemView.as_view(), 
         name='remove-procedure-item'),

    path('bills/<int:bill_id>/general-items/<int:item_id>/',
         RemoveGeneralItemView.as_view(),
         name='remove-general-item'),

    # ────────────────────────────────────────────────────────────────────────
    # PRESCRIPTION MEDICINES
    # ────────────────────────────────────────────────────────────────────────
    # ────────────────────────────────────────────────────────────────────────
    # PRESCRIPTION QUEUE (list) + PRESCRIPTION MEDICINES (detail)
    # ────────────────────────────────────────────────────────────────────────
    # GET /api/pharmacist/prescriptions/
    # Consultations with prescriptions sent to pharmacy, with dispensing status
    path('prescriptions/',
         PrescriptionQueueListView.as_view(),
         name='prescription-queue'),

    path('prescriptions/<int:prescription_id>/medicines/', 
         FetchPrescriptionMedicinesView.as_view(), 
         name='prescription-medicines'),

    # ────────────────────────────────────────────────────────────────────────
    # SUPPLIES & CONSUMABLES (non-dispensable stock: syringes, gloves, IV
    # sets, dressings, PPE, etc — separate stock system, lives in this app)
    # ────────────────────────────────────────────────────────────────────────
    path('supplies/dashboard/',
         SupplyDashboardSummaryView.as_view(),
         name='supply-dashboard'),

    path('supplies/items/',
         SupplyItemListView.as_view(),
         name='supply-item-list'),

    path('supplies/items/create/',
         SupplyItemCreateView.as_view(),
         name='supply-item-create'),

    path('supplies/items/<int:item_id>/',
         SupplyItemDetailView.as_view(),
         name='supply-item-detail'),

    path('supplies/items/<int:item_id>/update/',
         SupplyItemUpdateView.as_view(),
         name='supply-item-update'),

    path('supplies/items/<int:item_id>/add-stock/',
         AddStockView.as_view(),
         name='supply-add-stock'),

    path('supplies/items/<int:item_id>/use/',
         UseStockView.as_view(),
         name='supply-use-stock'),

    path('supplies/batches/<int:batch_id>/',
         SupplyBatchDetailView.as_view(),
         name='supply-batch-detail'),

    path('supplies/batches/<int:batch_id>/return/',
         ReturnToProviderView.as_view(),
         name='supply-batch-return'),

    path('supplies/returns/',
         SupplyReturnListView.as_view(),
         name='supply-return-list'),

    path('supplies/usage-logs/',
         UsageLogListView.as_view(),
         name='supply-usage-log-list'),

    path('supplies/alerts/',
         AlertListView.as_view(),
         name='supply-alert-list'),

    path('supplies/alerts/<int:alert_id>/resolve/',
         AlertResolveView.as_view(),
         name='supply-alert-resolve'),

    # ────────────────────────────────────────────────────────────────────────
    # GENERAL ITEMS (non-medicine retail products sold on pharmacy bills)
    # ────────────────────────────────────────────────────────────────────────
    path('general-items/',
         GeneralItemListView.as_view(),
         name='general-item-list'),

    path('general-items/create/',
         GeneralItemCreateView.as_view(),
         name='general-item-create'),

    path('general-items/search/',
         GeneralItemSearchView.as_view(),
         name='general-item-search'),

    path('general-items/<int:pk>/',
         GeneralItemDetailView.as_view(),
         name='general-item-detail'),

    path('general-items/<int:pk>/update/',
         GeneralItemUpdateView.as_view(),
         name='general-item-update'),

    path('general-item-batches/',
         GeneralItemBatchListView.as_view(),
         name='general-item-batch-list'),

    path('general-item-batches/create/',
         GeneralItemBatchCreateView.as_view(),
         name='general-item-batch-create'),

    path('general-item-batches/<int:pk>/',
         GeneralItemBatchDetailView.as_view(),
         name='general-item-batch-detail'),

    path('general-item-batches/<int:pk>/update/',
         GeneralItemBatchUpdateView.as_view(),
         name='general-item-batch-update'),

    path('general-item-alerts/',
         GeneralItemStockAlertListView.as_view(),
         name='general-item-alert-list'),

    path('general-item-alerts/<int:alert_id>/resolve/',
         GeneralItemStockAlertResolveView.as_view(),
         name='general-item-alert-resolve'),

    path('general-item-batches/<int:batch_id>/return/',
         GeneralItemReturnToProviderView.as_view(),
         name='general-item-return-to-provider'),

    path('general-item-returns/',
         GeneralItemReturnListView.as_view(),
         name='general-item-return-list'),
]