from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from datetime import date, timedelta

from reception.models import Patient, ConsultationBill
from reception.serializers import ConsultationBillSerializer
from administration.models import StaffProfile
from doctor.models import DoctorProfile


class ConsultationBillSerializerTestCase(TestCase):
    def setUp(self):
        # Create user and staff profile for the doctor
        self.doc_user = User.objects.create_user(
            username="testdoctor",
            first_name="Test",
            last_name="Doctor",
            password="testpassword"
        )
        self.doc_staff = StaffProfile.objects.create(
            user=self.doc_user,
            role="Doctor",
            date_of_birth=date(1990, 1, 1), # Over 25 years old
            phone="+919876543210"
        )
        self.doctor, _ = DoctorProfile.objects.get_or_create(staff=self.doc_staff)
        self.doctor.specialization = "Cardiology"
        self.doctor.consultation_fee = 500.00
        self.doctor.is_available = True
        self.doctor.save()

        # Create patient
        self.patient = Patient.objects.create(
            first_name="Jane",
            last_name="Doe",
            phone="9876543210",
            gender="Female",
            date_of_birth=date(1995, 5, 15)
        )

    def test_missing_doctor_name_with_doctor_profile_succeeds(self):
        """
        Omitting doctor_name in the input data when a doctor profile is supplied
        should succeed and automatically populate doctor_name from the profile.
        """
        data = {
            "patient": self.patient.patient_id,
            "doctor": self.doctor.profile_id,
            "consultation_fee": 500.00,
            "payment_method": "CASH",
            "consultation_type": "NEW"
        }
        serializer = ConsultationBillSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        
        # Save the bill and verify doctor_name was populated
        bill = serializer.save()
        self.assertEqual(bill.doctor_name, "Dr. Test Doctor")

    def test_upi_payment_requires_reference(self):
        """
        If payment_method is UPI, a valid upi_reference is required.
        """
        # Missing reference fails
        data = {
            "patient": self.patient.patient_id,
            "doctor": self.doctor.profile_id,
            "consultation_fee": 500.00,
            "payment_method": "UPI",
            "consultation_type": "NEW"
        }
        serializer = ConsultationBillSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("upi_reference", serializer.errors)

        # Supplying reference succeeds
        data["upi_reference"] = "UPI-TXN-12345"
        serializer = ConsultationBillSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_revisit_bill_requires_valid_window(self):
        """
        A REVISIT consultation requires an active revisit window (within 2 days
        of a previous NEW consultation).
        """
        # 1. No previous bill - should fail
        data = {
            "patient": self.patient.patient_id,
            "doctor": self.doctor.profile_id,
            "consultation_fee": 0.00,
            "payment_method": "CASH",
            "consultation_type": "REVISIT"
        }
        serializer = ConsultationBillSerializer(data=data)
        self.assertFalse(serializer.is_valid())
        self.assertIn("consultation_type", serializer.errors)

        # 2. Add a NEW bill
        new_bill = ConsultationBill.objects.create(
            patient=self.patient,
            doctor=self.doctor,
            doctor_name="Dr. Test Doctor",
            consultation_fee=500.00,
            consultation_type="NEW",
            payment_method="CASH",
            payment_status="PAID"
        )
        new_bill.revisit_valid_until = date.today() + timedelta(days=2)
        new_bill.save()

        # 3. Try creating revisit bill now - should succeed
        serializer = ConsultationBillSerializer(data=data)
        self.assertTrue(serializer.is_valid(), serializer.errors)

