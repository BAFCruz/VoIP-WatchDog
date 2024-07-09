Python scripts from an implementation of a Reputation System in a virtual lab with Asterisk for testing dynamic control of authentication and call control. 

The 3 scripts below require variables from Asterisk dial plan, which extracts SIP headers information (Call Routing part)
- Call Handler is triggered by Asterisk dial plan when an INVITE arrives to check both source and destination scores, if any < 5 the call is terminated using the CHANNEL.
- Call Tracker is also fed by Asterisk, tracks active calls originating from the internal network using a database table named active_calls, with the intent to prevent multiple calls from the same internal source
- Call Tracker Cleaner is triggered by Asterisk to clean the active_calls database table the moment a call is terminated

Not Asterisk dial plan related scripts (Authentication part)
- Security log Parser will read and interpret logs from the /etc/Asterisk/security.log for successful and failed login attempts.
- ACL Handler configures /etc/Asterisk/acl.conf, adding IPs if score > 5, or removing if score < 5, as per a database table.

Call Detail Records 
- CDR Parser automatically fetches call records when available in the Call Detail Record (CDR) database table looking through the uniqueid.
