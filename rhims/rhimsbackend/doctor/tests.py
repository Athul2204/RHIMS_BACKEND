from django.contrib.auth.models import User
from rest_framework.test import APITestCase
from rest_framework import status
from datetime import date
from django.utils import timezone

from administration.models import StaffProfile
from doctor.models import (
    DoctorProfile,
    Consultation,
    Prescription,
    PrescriptionItem,
    FollowUpReminder,
)
from reception.models import Patient
from pharmacist.models import Medicine, RouteChoices


class DoctorAPITestCase(APITestCase):
    def setUp(self):
        # Create users
        self.doctor_user = User.objects.create_user(
            username="testdoctor",
            first_name="Test",
            last_name="Doctor",
            password="testpassword"
        )
        self.admin_user = User.objects.create_superuser(
            username="adminuser",
            email="admin@example.com",
            password="adminpassword"
        )

        # Create doctor staff profile and retrieve/update the auto-created doctor profile
        self.doc_staff = StaffProfile.objects.create(
            user=self.doctor_user,
            role="Doctor",
            date_of_birth=date(1985, 5, 15),
            phone="+919876543210"
        )
        self.doctor_profile = DoctorProfile.objects.get(staff=self.doc_staff)
        self.doctor_profile.specialization = "Cardiology"
        self.doctor_profile.registration_number = "REG-12345"
        self.doctor_profile.department = "Cardiology"
        self.doctor_profile.consultation_fee = 600.00
        self.doctor_profile.is_available = True
        self.doctor_profile.save()

        # Create patient
        self.patient = Patient.objects.create(
            first_name="John",
            last_name="Doe",
            phone="9988776655",
            gender="Male",
            date_of_birth=date(1990, 8, 20)
        )

        # Create consultation
        self.consultation = Consultation.objects.create(
            patient=self.patient,
            doctor=self.doctor_profile,
            doctor_user=self.doctor_user,
            consultation_date=date.today(),
            chief_complaint="Chest pain"
        )

        # Create medicine
        self.medicine = Medicine.objects.create(
            name="Aspirin 75mg",
            generic_name="Aspirin",
            category="NSAID",
            unit="Tablet",
            medicine_type="TABLET",
            strength="75mg",
            default_route=RouteChoices.ORAL,
            is_active=True
        )

        # Create prescription
        self.prescription = Prescription.objects.create(
            consultation=self.consultation,
            prescribed_by=self.doctor_user,
            prescription_date=date.today(),
            notes="Take daily"
        )

        # Create prescription item
        self.prescription_item = PrescriptionItem.objects.create(
            prescription=self.prescription,
            medicine=self.medicine,
            dose_quantity=1.0,
            frequency="OD",
            duration_days=10,
            route=RouteChoices.ORAL,
            instructions="Take after breakfast"
        )

        # Create reminder
        self.reminder = FollowUpReminder.objects.create(
            patient=self.patient,
            consultation=self.consultation,
            followup_date=date.today() + timezone.timedelta(days=7),
            status="PENDING",
            reception_notes="Call to check up"
        )

        # Log in as doctor user by default
        self.client.force_authenticate(user=self.doctor_user)

    # ─────────────────────────────────────────────
    # DOCTOR PROFILE API TESTS
    # ─────────────────────────────────────────────

    def test_list_doctor_profiles(self):
        url = "/api/doctor/profiles/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data) >= 1)

    def test_get_doctor_profile(self):
        url = f"/api/doctor/profiles/{self.doctor_profile.doctor_id}/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["specialization"], "Cardiology")

    def test_create_doctor_profile(self):
        # Create a new doctor user first
        new_doc_user = User.objects.create_user(
            username="newdoctor",
            first_name="New",
            last_name="Doc",
            password="testpassword"
        )
        # Disconnect post_save signal temporarily so it doesn't auto-create DoctorProfile
        from django.db.models.signals import post_save
        from administration.signals import handle_staff_post_save
        post_save.disconnect(handle_staff_post_save, sender=StaffProfile)
        try:
            new_doc_staff = StaffProfile.objects.create(
                user=new_doc_user,
                role="Doctor",
                date_of_birth=date(1988, 10, 10),
                phone="+919876543211"
            )
        finally:
            post_save.connect(handle_staff_post_save, sender=StaffProfile)

        url = "/api/doctor/profiles/"
        data = {
            "staff": new_doc_staff.id,
            "specialization": "Neurology",
            "registration_number": "REG-54321",
            "department": "Neurology",
            "consultation_fee": "750.00",
            "is_available": True
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_patch_doctor_profile(self):
        url = f"/api/doctor/profiles/{self.doctor_profile.doctor_id}/"
        data = {"specialization": "Interventional Cardiology"}
        response = self.client.patch(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["specialization"], "Interventional Cardiology")

    def test_put_doctor_profile(self):
        url = f"/api/doctor/profiles/{self.doctor_profile.doctor_id}/"
        data = {
            "staff": self.doc_staff.id,
            "specialization": "Cardiothoracic Surgery",
            "registration_number": "REG-12345",
            "consultation_fee": "900.00",
            "is_available": False
        }
        response = self.client.put(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["specialization"], "Cardiothoracic Surgery")

    def test_delete_doctor_profile(self):
        url = f"/api/doctor/profiles/{self.doctor_profile.doctor_id}/"
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(DoctorProfile.objects.filter(doctor_id=self.doctor_profile.doctor_id).exists())

    # ─────────────────────────────────────────────
    # CONSULTATION API TESTS
    # ─────────────────────────────────────────────

    def test_list_consultations(self):
        url = "/api/doctor/consultations/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data) >= 1)

    def test_get_consultation(self):
        url = f"/api/doctor/consultations/{self.consultation.consultation_id}/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["chief_complaint"], "Chest pain")

    def test_create_consultation(self):
        url = "/api/doctor/consultations/create/"
        data = {
            "patient": self.patient.patient_id,
            "doctor": self.doctor_profile.doctor_id,
            "chief_complaint": "Follow-up",
            "status": "STARTED"
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_patch_consultation(self):
        url = f"/api/doctor/consultations/{self.consultation.consultation_id}/"
        data = {"chief_complaint": "Updated complaint"}
        response = self.client.patch(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["chief_complaint"], "Updated complaint")

    def test_put_consultation(self):
        url = f"/api/doctor/consultations/{self.consultation.consultation_id}/"
        data = {
            "patient": self.patient.patient_id,
            "doctor": self.doctor_profile.doctor_id,
            "chief_complaint": "Chest pain - severe",
            "status": "STARTED"
        }
        response = self.client.put(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["chief_complaint"], "Chest pain - severe")

    def test_delete_consultation(self):
        url = f"/api/doctor/consultations/{self.consultation.consultation_id}/"
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Consultation.objects.filter(consultation_id=self.consultation.consultation_id).exists())

    # ─────────────────────────────────────────────
    # PRESCRIPTION API TESTS
    # ─────────────────────────────────────────────

    def test_list_prescriptions(self):
        url = f"/api/doctor/consultations/{self.consultation.consultation_id}/prescriptions/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_create_prescription(self):
        url = f"/api/doctor/consultations/{self.consultation.consultation_id}/prescriptions/"
        data = {
            "prescription_type": "FINAL",
            "notes": "Drink plenty of water"
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_get_prescription(self):
        url = f"/api/doctor/prescriptions/{self.prescription.prescription_id}/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["notes"], "Take daily")

    def test_patch_prescription(self):
        url = f"/api/doctor/prescriptions/{self.prescription.prescription_id}/"
        data = {"notes": "Take daily with food"}
        response = self.client.patch(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["notes"], "Take daily with food")

    def test_put_prescription(self):
        url = f"/api/doctor/prescriptions/{self.prescription.prescription_id}/"
        data = {
            "prescription_type": "FINAL",
            "notes": "Fully revised notes"
        }
        response = self.client.put(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["notes"], "Fully revised notes")

    def test_delete_prescription(self):
        url = f"/api/doctor/prescriptions/{self.prescription.prescription_id}/"
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Prescription.objects.filter(prescription_id=self.prescription.prescription_id).exists())

    # ─────────────────────────────────────────────
    # PRESCRIPTION ITEM API TESTS
    # ─────────────────────────────────────────────

    def test_list_prescription_items(self):
        url = f"/api/doctor/prescriptions/{self.prescription.prescription_id}/items/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_create_prescription_item(self):
        url = f"/api/doctor/prescriptions/{self.prescription.prescription_id}/items/"
        data = {
            "medicine": self.medicine.medicine_id,
            "dose_quantity": 2.0,
            "frequency": "BD",
            "duration_days": 5,
            "route": RouteChoices.ORAL,
            "instructions": "Take twice daily"
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_get_prescription_item(self):
        url = f"/api/doctor/prescription-items/{self.prescription_item.item_id}/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["instructions"], "Take after breakfast")

    def test_patch_prescription_item(self):
        url = f"/api/doctor/prescription-items/{self.prescription_item.item_id}/"
        data = {"instructions": "Take with meals"}
        response = self.client.patch(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["instructions"], "Take with meals")

    def test_put_prescription_item(self):
        url = f"/api/doctor/prescription-items/{self.prescription_item.item_id}/"
        data = {
            "medicine": self.medicine.medicine_id,
            "dose_quantity": 1.0,
            "frequency": "OD",
            "duration_days": 10,
            "route": RouteChoices.ORAL,
            "instructions": "Completely updated instructions"
        }
        response = self.client.put(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["instructions"], "Completely updated instructions")

    def test_delete_prescription_item(self):
        url = f"/api/doctor/prescription-items/{self.prescription_item.item_id}/"
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(PrescriptionItem.objects.filter(item_id=self.prescription_item.item_id).exists())

    # ─────────────────────────────────────────────
    # FOLLOW-UP REMINDER API TESTS
    # ─────────────────────────────────────────────

    def test_list_reminders(self):
        url = "/api/doctor/followup-reminders/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data) >= 1)

    def test_create_reminder(self):
        url = "/api/doctor/followup-reminders/"
        data = {
            "patient": self.patient.patient_id,
            "consultation": self.consultation.consultation_id,
            "followup_date": date.today() + timezone.timedelta(days=14),
            "status": "PENDING",
            "reception_notes": "New follow up"
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_get_reminder(self):
        url = f"/api/doctor/followup-reminders/{self.reminder.reminder_id}/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reception_notes"], "Call to check up")

    def test_patch_reminder(self):
        url = f"/api/doctor/followup-reminders/{self.reminder.reminder_id}/"
        data = {"reception_notes": "Called, no response"}
        response = self.client.patch(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reception_notes"], "Called, no response")

    def test_put_reminder(self):
        url = f"/api/doctor/followup-reminders/{self.reminder.reminder_id}/"
        data = {
            "patient": self.patient.patient_id,
            "consultation": self.consultation.consultation_id,
            "followup_date": self.reminder.followup_date,
            "status": "CALLED",
            "reception_notes": "Fully updated notes"
        }
        response = self.client.put(url, data)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["reception_notes"], "Fully updated notes")

    def test_delete_reminder(self):
        url = f"/api/doctor/followup-reminders/{self.reminder.reminder_id}/"
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(FollowUpReminder.objects.filter(reminder_id=self.reminder.reminder_id).exists())
