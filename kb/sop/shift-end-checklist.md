# Store IT Shift End Checklist

**Purpose:** To ensure all critical IT systems are properly secured, verified, and prepared for overnight operations before the closing shift manager leaves the premises.

**Scope:** This procedure applies to all closing shift managers at ServeWell Hospitality locations responsible for store closure and IT system handoff.

**Last Updated:** 2025-10-01

---

## Procedure

### 1. Verify EOD Settlement Completion

- Access the POS system and navigate to **Daily Settlement Reports**
- Confirm that the EOD (End of Day) settlement process has completed successfully
- **Decision Point:**
  - If settlement status shows "COMPLETED": Proceed to Step 2
  - If settlement status shows "PENDING" or "FAILED":
    - Attempt to re-run settlement
    - If re-run fails after 2 attempts, escalate to L2 Support immediately (do not leave premises)
- Document the settlement completion time in the Shift Handover Log
- Take a screenshot of the EOD confirmation screen for records

### 2. Pause Online Orders

- Log into the online ordering management portal (if applicable to your location)
- Navigate to **Order Management > Intake Settings**
- Change status from "ACCEPTING ORDERS" to "PAUSED"
- **Decision Point:**
  - If pause is successful: Confirm status displays "PAUSED - Orders Suspended" and proceed to Step 3
  - If pause fails or portal is unresponsive: Document the error message and escalate to L2 Support
- Verify no new orders are appearing in the queue (wait 2 minutes and refresh)
- Note: Failure to pause orders may result in unattended orders during closed hours

### 3. Check Printer Paper Levels

- Physically inspect all receipt printers at POS terminals (main counter, kitchen display, bar)
- For each printer, check the paper roll:
  - **Adequate (75-100%):** No action required; document "OK"
  - **Low (25-74%):** Replace paper roll before end of shift; document "REPLACED"
  - **Critical (0-24%) or Empty:** Replace immediately; document "REPLACED - CRITICAL"
- Test each printer by initiating a test print from the POS system
- **Decision Point:**
  - If any printer fails to print: Document which printer, restart that terminal (Step 4), and re-test
  - If printer still fails after restart: Escalate to L2 Support before leaving

### 4. Execute Nightly POS Restart (v4.x Systems Only)

- **Scope Check:** Confirm your location is running POS v4.x (check System Settings > About)
- If running v5.x or higher, skip to Step 5
- Perform the following sequence:
  1. Ensure no transactions are in progress (all registers closed)
  2. Navigate to **System Settings > Maintenance > Nightly Shutdown**
  3. Select **"Schedule Nightly Restart"** and confirm time is set to 00:30 (12:30 AM)
  4. Click **"Confirm Restart Schedule"** — system will display confirmation message with restart time
  5. Document the restart scheduling in the Shift Handover Log
- **Decision Point:**
  - If restart scheduling succeeds: Proceed to Step 5
  - If restart scheduling fails:
    - Try manual restart: Power down all terminals, wait 2 minutes, power on main server first, then terminals
    - If manual restart encounters issues, escalate to L2 Support (provide error codes)
- Allow 5 minutes for systems to fully boot after restart before confirming stability

### 5. File Incident Report (If Applicable)

- **Trigger:** File a report if ANY of the following occurred during the shift:
  - System downtime or unplanned outages
  - Failed transactions or settlement errors
  - Hardware malfunction (printer, terminal, network equipment)
  - Security incidents or suspicious activity
  - Network connectivity issues
  - Data entry errors affecting POS records
- Access **IT Portal > Incident Management > New Report**
- Complete the following required fields:
  - **Incident Type:** Select from dropdown (System, Hardware, Network, Security, Other)
  - **Severity Level:**
    - Critical (system down, unable to process transactions)
    - High (affects multiple terminals or functions)
    - Medium (isolated issue, workaround available)
    - Low (minor issue, no impact on operations)
  - **Description:** Provide specific details—what happened, when, which systems affected, steps already taken
  - **Time Occurred:** Record exact time of incident
  - **Resolution Status:** Mark as "Open" if unresolved; "Resolved" if issue was fixed during shift
  - **Attachments:** Include screenshots of error messages if available
- Submit the report and note the **Incident ID** in the Shift Handover Log
- **Decision Point:**
  - If severity is "Critical": Contact On-Call Manager immediately (do not wait)
  - If severity is "High": Escalate to L2 Support via email with incident ID
  - If severity is "Medium" or "Low": Report will be reviewed during next business day

---

## Required Information / Checklist

### Before Leaving the Store

- [ ] **EOD Settlement:** Status verified as COMPLETED; screenshot/confirmation retained
- [ ] **Online Orders:** Status confirmed as PAUSED in system; no new orders visible
- [ ] **Printer 1 (Main Counter):** Paper level checked; status: ****\_\_\_****
- [ ] **Printer 2 (Kitchen Display):** Paper level checked; status: ****\_\_\_****
- [ ] **Printer 3 (Bar/Secondary):** Paper level checked; status: ****\_\_\_**** (if applicable)
- [ ] **Test Prints:** All functioning printers confirmed with successful test print
- [ ] **POS Version Confirmed:** v****\_\_**** (if v4.x, nightly restart scheduled at 00:30)
- [ ] **Nightly Restart Scheduled:** Yes / No / N/A (v5.x+)
- [ ] **Incidents Occurred:** Yes / No
- [ ] **Incident Reports Filed:** Count: ****\_\_**** | Incident IDs: ****\_\_****
- [ ] **Shift Handover Log:** Completed and signed by closing manager
- [ ] **Date & Time:** ********\_\_\_********
- [ ] **Closing Manager Name:** ********\_\_\_********
- [ ] **Closing Manager Signature:** ********\_\_\_********

### Information to Collect for Escalation

- Error messages (exact text or screenshot)
- Affected POS terminal(s) or system(s)
- Time issue occurred
- Steps already attempted
- Current system status
- Incident ID (if report filed)

---

## Escalation Contacts

| Role                | Contact             | Response Time                                            |
| ------------------- | ------------------- | -------------------------------------------------------- |
| L2 Support          | it-l2@servewell.in  | 1 hour (business hours); next business day (after hours) |
| On-Call Manager     | oncall@servewell.in | 15 minutes (critical incidents only)                     |
| IT Helpdesk Hotline | +91-XXXX-XXXX-XXXX  | Available 24/7 for critical outages                      |

---

## Notes

- **Critical Reminder:** Do not leave the premises until all steps are completed and verified. Incomplete EOD settlement or system failures must be escalated immediately.
- **For v5.x Systems:** Automatic nightly restarts are configured by IT—manual scheduling not required.
- **Paper Stock:** Ensure adequate spare rolls are available at the location. Order supplies if stock is low.
- **Retention:** Keep completed checklists for 30 days for audit purposes.
