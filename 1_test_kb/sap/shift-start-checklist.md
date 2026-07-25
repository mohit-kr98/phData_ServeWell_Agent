# Store IT Shift Start Checklist

**Purpose:** To verify all critical IT systems are operational before store opening to ensure seamless customer service and transaction processing.

**Scope:** This procedure applies to all opening shift managers at ServeWell Hospitality locations.

**Last Updated:** 2025-10-01

**Expected Completion Time:** 5-7 minutes

---

## Procedure

### 1. Network Connectivity Test

1. Access the network status dashboard on the manager terminal or visit the IT Status Portal
2. Perform a ping test to the primary server:
   - Open Command Prompt/Terminal
   - Execute: `ping server.servewell.local`
   - Confirm response time is <100ms with 0% packet loss
3. **Decision Point:** If ping fails or exceeds 100ms:
   - Restart the store router (wait 2 minutes for full reboot)
   - Perform ping test again
   - If still failing → Escalate to L2 Support immediately (see Escalation Contacts)
4. Document result in the Daily IT Log

### 2. POS System Test Transaction

1. Log into the primary POS terminal using your manager credentials
2. Initiate a test transaction:
   - Ring up any single item (e.g., coffee)
   - Enter test amount of ₹1.00
   - Process payment using test card (Card #: 4111111111111111, expiry: 12/25, CVV: 123)
3. Confirm transaction completion with receipt print
4. **Decision Point:** If transaction fails or times out:
   - Clear POS cache: Restart the POS terminal
   - Attempt test transaction again
   - If payment gateway error persists → Escalate to L2 Support
5. Void the test transaction immediately after confirmation
6. Document result in the Daily IT Log

### 3. Printer Network Test Print

1. Access printer settings from any POS terminal (Menu → System Settings → Printers)
2. Select the main receipt printer and execute "Test Print"
3. Confirm test page prints within 10 seconds with clear, legible output
4. Check for the following on printed page:
   - ServeWell logo visible
   - Date/time stamp accurate
   - All text aligned properly (no cut-off edges)
5. **Decision Point:** If test print fails or is illegible:
   - Check physical printer for paper jam or low toner
   - Restart the printer (power cycle: 30 seconds off, then on)
   - Attempt test print again
   - If issue persists → Submit ticket to L2 Support with photo of failed print
6. Document result in the Daily IT Log

### 4. Online Orders System Enabled Check

1. Access the Online Orders Management portal from manager terminal
2. Verify the following statuses:
   - **Order Acceptance:** Enabled ✓
   - **Store Status:** Online/Open ✓
   - **Delivery Integration:** Connected ✓
   - **Last Sync Time:** Within last 5 minutes ✓
3. **Decision Point:** If any status shows disabled or "Last Sync" exceeds 5 minutes:
   - Manually sync the system: Click "Sync Now" button
   - Wait 30 seconds for sync to complete
   - Verify all statuses show green/enabled
   - If sync fails → Escalate to L2 Support with screenshot of error
4. Document result in the Daily IT Log

### 5. Kiosk Health Check

1. Walk to each ordering kiosk (if applicable to your location)
2. For each kiosk, verify:
   - Screen is powered on and displays menu clearly
   - Touch screen responds to test touches on all four corners and center
   - No error messages or blank screens visible
3. **Decision Point:** If any kiosk is unresponsive or shows errors:
   - Perform soft restart: Hold power button for 10 seconds
   - Wait 2 minutes for reboot
   - Test screen responsiveness again
   - If kiosk remains non-functional → Mark as "Out of Service" via manager portal and escalate to L2 Support
4. Document result in the Daily IT Log (including which kiosk, if any, is offline)

### 6. Final Confirmation

1. Review all five sections in the Daily IT Log
2. Confirm all items show "PASS" status
3. **Decision Point:** If any item shows "FAIL":
   - Do not open store to customers until all systems pass
   - Contact On-Call Manager immediately
4. Sign off on the Daily IT Log with your name, time, and date
5. Proceed with store opening

---

## Required Information / Checklist

### Network Connectivity

- [ ] Ping test to `server.servewell.local` successful (response <100ms, 0% packet loss)
- [ ] Document actual response time: **\_\_\_\_** ms
- [ ] Troubleshooting performed (if applicable): ********\_\_********

### POS System

- [ ] Manager login successful
- [ ] Test transaction completed and voided
- [ ] Transaction processing time: **\_\_\_\_** seconds
- [ ] Receipt printed legibly
- [ ] Any errors encountered: ********\_\_********

### Printer

- [ ] Test print executed
- [ ] Print quality verified (legible, properly aligned, no jams)
- [ ] Paper level adequate (>20% full)
- [ ] Toner/ink level status: ********\_\_********

### Online Orders

- [ ] Order Acceptance: Enabled ✓
- [ ] Store Status: Online/Open ✓
- [ ] Delivery Integration: Connected ✓
- [ ] Last Sync Time: **\_\_\_\_** (must be ≤5 minutes ago)
- [ ] Any pending order errors: ********\_\_********

### Kiosks (if applicable)

- [ ] Kiosk 1: Status (Pass/Fail/Out of Service)
- [ ] Kiosk 2: Status (Pass/Fail/Out of Service)
- [ ] Kiosk 3: Status (Pass/Fail/Out of Service)
- [ ] Touch responsiveness verified on all active kiosks: Yes / No
- [ ] Any error messages: ********\_\_********

### Overall Sign-Off

- [ ] All five system checks completed
- [ ] All items marked PASS
- [ ] Manager Name: ********\_\_********
- [ ] Time Completed: ********\_\_********
- [ ] Date: ********\_\_********
- [ ] Manager Signature: ********\_\_********

---

## Escalation Contacts

| Role                    | Contact             | Response Time |
| ----------------------- | ------------------- | ------------- |
| L2 IT Support           | it-l2@servewell.in  | 15 minutes    |
| L2 IT Support (Phone)   | +91-XXXX-XXXX-XXX   | Immediate     |
| On-Call Manager         | oncall@servewell.in | 10 minutes    |
| On-Call Manager (Phone) | +91-XXXX-XXXX-XXX   | Immediate     |

**Escalation Trigger:** Contact L2 Support or On-Call Manager if any system fails to pass after troubleshooting steps. Do not open store until all systems pass.
