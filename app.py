from flask import Flask, render_template, redirect, url_for, request, flash, jsonify, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from datetime import datetime
import os
import re

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

app = Flask(__name__)
app.config['SECRET_KEY'] = 'organ-donation-secret-key-2024'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(os.path.dirname(os.path.abspath(__file__)), 'instance', 'organ_donation.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = ''

ORGANS = [
    'Heart', 'Kidney', 'Liver', 'Lung', 'Pancreas',
    'Intestine', 'Cornea', 'Bone Marrow', 'Skin'
]

BLOOD_GROUPS = ['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-']

BLOOD_COMPATIBILITY = {
    'A+':  ['A+', 'A-', 'O+', 'O-'],
    'A-':  ['A-', 'O-'],
    'B+':  ['B+', 'B-', 'O+', 'O-'],
    'B-':  ['B-', 'O-'],
    'AB+': ['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-'],
    'AB-': ['A-', 'B-', 'AB-', 'O-'],
    'O+':  ['O+', 'O-'],
    'O-':  ['O-'],
}


# ─── Models ───────────────────────────────────────────────────────────────────

class Account(db.Model, UserMixin):
    """One account per person (identified by email)."""
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(100), nullable=False)
    email        = db.Column(db.String(120), unique=True, nullable=False)
    password     = db.Column(db.String(200), nullable=False)
    phone        = db.Column(db.String(20), nullable=False)
    age          = db.Column(db.Integer, nullable=False)
    blood_group  = db.Column(db.String(5), nullable=False)
    city         = db.Column(db.String(100), nullable=False)
    state        = db.Column(db.String(100), nullable=False)
    gender       = db.Column(db.String(10), nullable=False, default='Other')
    is_admin     = db.Column(db.Boolean, default=False)
    is_active_user = db.Column(db.Boolean, default=True)
    registered_on  = db.Column(db.DateTime, default=datetime.utcnow)

    organ_entries  = db.relationship('UserOrgan', backref='account', lazy=True, cascade='all, delete-orphan')
    notifications  = db.relationship('Notification', backref='account', lazy=True, cascade='all, delete-orphan')


class UserOrgan(db.Model):
    """Each row = one (role, organ) combination for an account."""
    id         = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    role       = db.Column(db.String(10), nullable=False)   # 'donor' or 'receiver'
    organ      = db.Column(db.String(50), nullable=False)
    is_active  = db.Column(db.Boolean, default=True)
    added_on   = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('account_id', 'role', 'organ', name='uq_account_role_organ'),)


class Notification(db.Model):
    id         = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('account.id'), nullable=False)
    message    = db.Column(db.String(500), nullable=False)
    sent_on    = db.Column(db.DateTime, default=datetime.utcnow)
    status     = db.Column(db.String(20), default='unread')


@login_manager.user_loader
def load_user(user_id):
    return Account.query.get(int(user_id))


# ─── Helpers ──────────────────────────────────────────────────────────────────

def find_matches_for_entry(entry, account):
    """Return list of (Account, UserOrgan) tuples that match a given UserOrgan entry."""
    matches = []
    if entry.role == 'donor':
        receivers = UserOrgan.query.filter_by(role='receiver', organ=entry.organ, is_active=True).all()
        for r in receivers:
            if r.account_id == account.id:
                continue
            if account.blood_group in BLOOD_COMPATIBILITY.get(r.account.blood_group, []):
                matches.append((r.account, r))
    else:
        donors = UserOrgan.query.filter_by(role='donor', organ=entry.organ, is_active=True).all()
        for d in donors:
            if d.account_id == account.id:
                continue
            if d.account.blood_group in BLOOD_COMPATIBILITY.get(account.blood_group, []):
                matches.append((d.account, d))
    return matches


def find_all_matches(account):
    """All matches across every organ entry of this account."""
    seen = set()
    results = []
    for entry in account.organ_entries:
        if not entry.is_active:
            continue
        for (matched_acc, matched_entry) in find_matches_for_entry(entry, account):
            key = (matched_acc.id, matched_entry.id)
            if key not in seen:
                seen.add(key)
                results.append({
                    'account': matched_acc,
                    'entry': matched_entry,
                    'my_entry': entry,
                })
    return results


def notify_new_entry(account, entry):
    """Create notifications and emails for both sides when a new organ entry is added.

    Strategy:
    - Each matched person (existing side) gets one individual email per match.
    - The registering account gets ONE consolidated email listing all matches.
    - All emails are sent over a single reused SMTP connection to avoid
      Gmail rate-limiting when there are many matches.
    """
    matches = find_matches_for_entry(entry, account)
    if not matches:
        return

    self_lines = []
    # List of (to_email, subject, body) to send in one SMTP session
    email_queue = []

    for (matched_acc, matched_entry) in matches:
        if entry.role == 'donor':
            # Message to the existing receiver: a new donor matched them
            msg_to_match = (
                f"🎉 Match Found! A donor matched your {matched_entry.organ} request. "
                f"Donor: {account.name} | Blood Group: {account.blood_group} | "
                f"City: {account.city} | Contact: {account.phone}"
            )
            # Message to the new donor: they matched a receiver
            self_line = (
                f"🎉 Match Found! Your {entry.organ} donation matches a recipient. "
                f"Recipient: {matched_acc.name} | Blood Group: {matched_acc.blood_group} | "
                f"City: {matched_acc.city} | Contact: {matched_acc.phone}"
            )
        else:
            # Message to the existing donor: a new receiver matched them
            msg_to_match = (
                f"🎉 Match Found! A recipient matched your {matched_entry.organ} donation. "
                f"Recipient: {account.name} | Blood Group: {account.blood_group} | "
                f"City: {account.city} | Contact: {account.phone}"
            )
            # Message to the new receiver: they matched a donor
            self_line = (
                f"🎉 Match Found! Your {entry.organ} request matches a donor. "
                f"Donor: {matched_acc.name} | Blood Group: {matched_acc.blood_group} | "
                f"City: {matched_acc.city} | Contact: {matched_acc.phone}"
            )

        # Notify the matched user about this new match
        db.session.add(Notification(account_id=matched_acc.id, message=msg_to_match))
        self_lines.append(self_line)
        email_queue.append((matched_acc.email, '🫀 Organ Donation Match Found!', msg_to_match))

    # Notify the registering user — one notification per match so they see each one clearly
    for line in self_lines:
        db.session.add(Notification(account_id=account.id, message=line))

    # One consolidated email to the registering account
    if len(self_lines) == 1:
        consolidated_body = f"Match Found!\n\n{self_lines[0]}"
    else:
        items = "\n".join(f"{i+1}. {line}" for i, line in enumerate(self_lines))
        consolidated_body = (
            f"You have {len(self_lines)} match(es) for your {entry.role} entry ({entry.organ}):\n\n{items}"
        )
    email_queue.append((account.email, '🫀 Organ Donation Match Found!', consolidated_body))

    db.session.commit()

    # Send all emails over a single SMTP connection
    send_bulk_emails(email_queue)


def send_bulk_emails(email_list):
    """Send multiple emails over a single SMTP connection.
    email_list: list of (to_email, subject, body) tuples.
    """
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    SENDER_EMAIL = "organdonation121@gmail.com"
    SENDER_PASSWORD = "rfbqkdyoyorkkrbf"

    try:
        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            for (to_email, subject, body) in email_list:
                try:
                    msg = MIMEMultipart()
                    msg['From'] = SENDER_EMAIL
                    msg['To'] = to_email
                    msg['Subject'] = subject
                    msg.attach(MIMEText(body, 'plain'))
                    server.sendmail(SENDER_EMAIL, to_email, msg.as_string())
                    print(f"Email sent to {to_email}")
                except Exception as e:
                    print(f"Email error for {to_email}: {e}")
    except Exception as e:
        print(f"SMTP connection error: {e}")


def send_email_notification(to_email, subject, body):
    """Single email helper used by notify_match and admin routes."""
    send_bulk_emails([(to_email, subject, body)])
    return True


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    donor_count    = UserOrgan.query.filter_by(role='donor').count()
    receiver_count = UserOrgan.query.filter_by(role='receiver').count()
    return render_template('index.html', donor_count=donor_count,
                           receiver_count=receiver_count, organs=ORGANS)


import re

def validate_password(password):
    if len(password) < 8:
        return 'Password must be at least 8 characters.'
    if not re.search(r'[A-Z]', password):
        return 'Password must contain at least one uppercase letter.'
    if not re.search(r'[0-9]', password):
        return 'Password must contain at least one number.'
    if not re.search(r'[!@#$%^&*(),.?":{}|<>_\-]', password):
        return 'Password must contain at least one special character.'
    return None


@app.route('/register', methods=['GET', 'POST'])
def register():
    # ── Already logged in ──
    if current_user.is_authenticated:
        if request.method == 'POST':
            email    = request.form.get('email', '').strip()
            password = request.form.get('password', '').strip()

            # Different email = create a brand new account (keep current session)
            if email and email != current_user.email:
                existing = Account.query.filter_by(email=email).first()
                if existing:
                    flash('An account with this email already exists. Please login.', 'info')
                    return redirect(url_for('login'))

                pw_error = validate_password(password)
                if pw_error:
                    flash(pw_error, 'danger')
                    return render_template('register.html', organs=ORGANS, blood_groups=BLOOD_GROUPS, add_entry_only=False)

                name        = request.form.get('name', '').strip()
                phone       = request.form.get('phone', '').strip()
                age         = request.form.get('age', '').strip()
                blood_group = request.form.get('blood_group', '').strip()
                city        = request.form.get('city', '').strip()
                state       = request.form.get('state', '').strip()
                gender      = request.form.get('gender', 'Other').strip()
                role        = request.form.get('role', '').strip()
                organ       = request.form.get('organ', '').strip()

                hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
                new_account = Account(
                    name=name, email=email, password=hashed_pw, phone=phone,
                    age=int(age), blood_group=blood_group, city=city,
                    state=state, gender=gender
                )
                db.session.add(new_account)
                db.session.flush()
                entry = UserOrgan(account_id=new_account.id, role=role, organ=organ)
                db.session.add(entry)
                db.session.commit()
                notify_new_entry(new_account, entry)
                flash(f'New account created for {email}. You are still logged in as {current_user.name}.', 'success')
                return redirect(url_for('dashboard'))

            # Same email = add a new organ entry to current account
            role  = request.form.get('role', '').strip()
            organ = request.form.get('organ', '').strip()
            dup = UserOrgan.query.filter_by(
                account_id=current_user.id, role=role, organ=organ).first()
            if dup:
                flash(f'You already have a {role} entry for {organ}.', 'info')
            else:
                entry = UserOrgan(account_id=current_user.id, role=role, organ=organ)
                db.session.add(entry)
                db.session.commit()
                notify_new_entry(current_user, entry)
                session['active_role']  = role
                session['active_organ'] = organ
                flash(f'New entry added: {role.capitalize()} for {organ}.', 'success')
            return redirect(url_for('dashboard'))
        else:
            return render_template('register.html', organs=ORGANS, blood_groups=BLOOD_GROUPS, add_entry_only=False)

    if request.method == 'POST':
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        role     = request.form.get('role', '').strip()
        organ    = request.form.get('organ', '').strip()

        # Same email → redirect to login
        existing = Account.query.filter_by(email=email).first()
        if existing:
            flash('An account with this email already exists. Please login.', 'info')
            return redirect(url_for('login'))

        # Password strength validation
        pw_error = validate_password(password)
        if pw_error:
            flash(pw_error, 'danger')
            return render_template('register.html', organs=ORGANS, blood_groups=BLOOD_GROUPS, add_entry_only=False)

        # ── New user ──
        name        = request.form.get('name', '').strip()
        phone       = request.form.get('phone', '').strip()
        age         = request.form.get('age', '').strip()
        blood_group = request.form.get('blood_group', '').strip()
        city        = request.form.get('city', '').strip()
        state       = request.form.get('state', '').strip()
        gender      = request.form.get('gender', 'Other').strip()

        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        account = Account(
            name=name, email=email, password=hashed_pw, phone=phone,
            age=int(age), blood_group=blood_group, city=city,
            state=state, gender=gender
        )
        db.session.add(account)
        db.session.flush()

        entry = UserOrgan(account_id=account.id, role=role, organ=organ)
        db.session.add(entry)
        db.session.commit()
        notify_new_entry(account, entry)

        login_user(account, remember=True)
        flash('Welcome! Your account has been created.', 'success')
        return redirect(url_for('dashboard'))

    return render_template('register.html', organs=ORGANS, blood_groups=BLOOD_GROUPS, add_entry_only=False)


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        account = Account.query.filter_by(email=email).first()
        if not account:
            flash('No account found with that email.', 'danger')
            return render_template('forgot_password.html')
        session['reset_email'] = email
        return redirect(url_for('reset_password'))
    return render_template('forgot_password.html')


@app.route('/reset-password', methods=['GET', 'POST'])
def reset_password():
    email = session.get('reset_email')
    if not email:
        flash('Please enter your email first.', 'danger')
        return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        new_password = request.form.get('password', '').strip()
        confirm      = request.form.get('confirm_password', '').strip()
        if new_password != confirm:
            flash('Passwords do not match.', 'danger')
            return render_template('reset_password.html')
        pw_error = validate_password(new_password)
        if pw_error:
            flash(pw_error, 'danger')
            return render_template('reset_password.html')
        account = Account.query.filter_by(email=email).first()
        account.password = bcrypt.generate_password_hash(new_password).decode('utf-8')
        db.session.commit()
        session.pop('reset_email', None)
        flash('Password updated successfully. Please login.', 'success')
        return redirect(url_for('login'))
    return render_template('reset_password.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        role     = request.form.get('role', '').strip()
        organ    = request.form.get('organ', '').strip()

        account = Account.query.filter_by(email=email).first()
        if account and bcrypt.check_password_hash(account.password, password):
            # Check if this role+organ combo exists, if not add it
            entry = UserOrgan.query.filter_by(
                account_id=account.id, role=role, organ=organ).first()
            if not entry:
                entry = UserOrgan(account_id=account.id, role=role, organ=organ)
                db.session.add(entry)
                db.session.commit()
                notify_new_entry(account, entry)
                flash(f'New entry added: {role.capitalize()} for {organ}.', 'success')

            session['active_role']  = role
            session['active_organ'] = organ
            login_user(account, remember=True)
            if account.is_admin:
                return redirect(url_for('admin_dashboard'))
            return redirect(url_for('dashboard'))

        flash('Invalid email or password.', 'danger')

    return render_template('login.html', organs=ORGANS)


@app.route('/logout')
@login_required
def logout():
    account_id = current_user.id
    session.pop('active_role', None)
    session.pop('active_organ', None)
    logout_user()
    Notification.query.filter_by(account_id=account_id).delete()
    UserOrgan.query.filter_by(account_id=account_id).delete()
    Account.query.filter_by(id=account_id).delete()
    db.session.commit()
    flash('Your account and all data have been permanently deleted.', 'info')
    return redirect(url_for('index'))


@app.route('/dashboard')
@login_required
def dashboard():

    active_role  = session.get('active_role')
    active_organ = session.get('active_organ')

    total_donors     = db.session.query(Account.id).join(UserOrgan, Account.id == UserOrgan.account_id).filter(
        UserOrgan.role == 'donor', UserOrgan.is_active == True, Account.is_admin == False).distinct().count()
    total_recipients = db.session.query(Account.id).join(UserOrgan, Account.id == UserOrgan.account_id).filter(
        UserOrgan.role == 'receiver', UserOrgan.is_active == True, Account.is_admin == False).distinct().count()
    total_users = Account.query.filter_by(is_admin=False).count()

    # Count total unique matches across all donors
    all_donor_entries = UserOrgan.query.filter_by(role='donor', is_active=True).all()
    seen_pairs = set()
    for e in all_donor_entries:
        for (matched_acc, matched_entry) in find_matches_for_entry(e, e.account):
            pair = tuple(sorted([e.id, matched_entry.id]))
            seen_pairs.add(pair)
    total_matches = len(seen_pairs)

    donor_list     = db.session.query(Account, UserOrgan).join(
        UserOrgan, Account.id == UserOrgan.account_id).filter(
        UserOrgan.role == 'donor', UserOrgan.is_active == True,
        Account.is_admin == False, Account.is_active_user == True
    ).order_by(Account.registered_on.desc()).all()

    recipient_list = db.session.query(Account, UserOrgan).join(
        UserOrgan, Account.id == UserOrgan.account_id).filter(
        UserOrgan.role == 'receiver', UserOrgan.is_active == True,
        Account.is_admin == False, Account.is_active_user == True
    ).order_by(Account.registered_on.desc()).all()

    matches       = find_all_matches(current_user)
    notifications = Notification.query.filter_by(
        account_id=current_user.id).order_by(Notification.sent_on.desc()).limit(10).all()

    return render_template('dashboard.html',
        matches=matches,
        notifications=notifications,
        total_donors=total_donors,
        total_recipients=total_recipients,
        total_matches=total_matches,
        total_users=total_users,
        donor_list=donor_list,
        recipient_list=recipient_list,
        active_role=active_role,
        active_organ=active_organ,
        organ_entries=current_user.organ_entries,
    )


@app.route('/users')
@login_required
def users():
    donors    = db.session.query(Account, UserOrgan).join(
        UserOrgan, Account.id == UserOrgan.account_id).filter(
        UserOrgan.role == 'donor', UserOrgan.is_active == True,
        Account.is_admin == False).all()
    receivers = db.session.query(Account, UserOrgan).join(
        UserOrgan, Account.id == UserOrgan.account_id).filter(
        UserOrgan.role == 'receiver', UserOrgan.is_active == True,
        Account.is_admin == False).all()
    return render_template('users.html', donors=donors, receivers=receivers)


@app.route('/delete_entry/<int:entry_id>', methods=['POST'])
@login_required
def delete_entry(entry_id):
    entry = UserOrgan.query.get_or_404(entry_id)
    # Only the owner or admin can delete
    if entry.account_id != current_user.id and not current_user.is_admin:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    db.session.delete(entry)
    db.session.commit()
    flash(f'Entry deleted successfully.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/notify_match/<int:entry_id>', methods=['POST'])
@login_required
def notify_match(entry_id):
    matched_entry = UserOrgan.query.get_or_404(entry_id)
    matched_acc   = matched_entry.account
    active_role   = session.get('active_role', current_user.organ_entries[0].role if current_user.organ_entries else 'donor')
    active_organ  = session.get('active_organ', current_user.organ_entries[0].organ if current_user.organ_entries else '')

    if active_role == 'donor':
        msg = (f"You have a match! Donor {current_user.name} "
               f"(Blood: {current_user.blood_group}, Organ: {active_organ}) "
               f"wants to connect. Contact: {current_user.phone}")
    else:
        msg = (f"You have a match! Receiver {current_user.name} "
               f"(Blood: {current_user.blood_group}, Organ: {active_organ}) "
               f"needs your help. Contact: {current_user.phone}")

    db.session.add(Notification(account_id=matched_acc.id, message=msg))
    db.session.commit()
    send_email_notification(matched_acc.email, '🫀 Organ Donation - New Message', msg)
    flash(f'Notification sent to {matched_acc.name}!', 'success')
    return redirect(url_for('dashboard'))


# ─── Admin Routes ──────────────────────────────────────────────────────────────

@app.route('/admin')
@login_required
def admin_dashboard():
    if not current_user.is_admin:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    total_users   = Account.query.filter_by(is_admin=False).count()
    donors        = UserOrgan.query.filter_by(role='donor').count()
    receivers     = UserOrgan.query.filter_by(role='receiver').count()
    notifications = Notification.query.count()
    recent_users  = Account.query.filter_by(is_admin=False).order_by(Account.registered_on.desc()).limit(5).all()
    organ_stats   = {}
    for organ in ORGANS:
        organ_stats[organ] = {
            'donors':    UserOrgan.query.filter_by(organ=organ, role='donor').count(),
            'receivers': UserOrgan.query.filter_by(organ=organ, role='receiver').count()
        }
    return render_template('admin/dashboard.html',
        total_users=total_users, donors=donors, receivers=receivers,
        notifications=notifications, recent_users=recent_users, organ_stats=organ_stats)


@app.route('/admin/users')
@login_required
def admin_users():
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
    all_accounts = Account.query.filter_by(is_admin=False).order_by(Account.registered_on.desc()).all()
    return render_template('admin/users.html', users=all_accounts)


@app.route('/admin/toggle_user/<int:user_id>', methods=['POST'])
@login_required
def toggle_user(user_id):
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
    account = Account.query.get_or_404(user_id)
    account.is_active_user = not account.is_active_user
    db.session.commit()
    flash(f'User {"activated" if account.is_active_user else "deactivated"} successfully.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/delete_user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
    account = Account.query.get_or_404(user_id)
    db.session.delete(account)
    db.session.commit()
    flash('User deleted successfully.', 'success')
    return redirect(url_for('admin_users'))


@app.route('/admin/matches')
@login_required
def admin_matches():
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
    all_matches = []
    donor_entries = UserOrgan.query.filter_by(role='donor', is_active=True).all()
    for entry in donor_entries:
        for (matched_acc, matched_entry) in find_matches_for_entry(entry, entry.account):
            all_matches.append({'donor': entry.account, 'donor_entry': entry,
                                 'receiver': matched_acc, 'receiver_entry': matched_entry})
    return render_template('admin/matches.html', matches=all_matches)


@app.route('/admin/send_notification/<int:user_id>', methods=['POST'])
@login_required
def admin_send_notification(user_id):
    if not current_user.is_admin:
        return redirect(url_for('dashboard'))
    account = Account.query.get_or_404(user_id)
    message = request.form.get('message', '')
    if message:
        db.session.add(Notification(account_id=account.id, message=message))
        db.session.commit()
        flash(f'Notification sent to {account.name}.', 'success')
    return redirect(url_for('admin_users'))


# ─── Notification API ─────────────────────────────────────────────────────────

@app.route('/notifications')
@login_required
def notifications_page():
    notifs = Notification.query.filter(
        Notification.account_id == current_user.id
    ).order_by(Notification.sent_on.desc()).all()
    return render_template('notifications.html', notifications=notifs)


@app.route('/api/notifications')
@login_required
def api_notifications():
    unread = Notification.query.filter(
        Notification.account_id == current_user.id,
        Notification.status == 'unread'
    ).order_by(Notification.sent_on.desc()).all()
    return jsonify({
        'unread': len(unread),
        'notifications': [{'id': n.id, 'message': n.message,
                           'time': n.sent_on.strftime('%d %b %Y, %I:%M %p')} for n in unread]
    })


@app.route('/api/notifications/read', methods=['POST', 'GET'])
@login_required
def mark_notifications_read():
    Notification.query.filter(
        Notification.account_id == current_user.id,
        Notification.status == 'unread'
    ).update({'status': 'read'})
    db.session.commit()
    return redirect(url_for('notifications_page'))


# ─── Chatbot ──────────────────────────────────────────────────────────────────

CHAT_RESPONSES = {
    # Greetings
    "hello": "Hello! 👋 I'm your Organ Donation Assistant. Ask me about organs, blood groups, registration, matching, or donation myths. Type <strong>help</strong> to see all topics.",
    "hi": "Hi there! 😊 How can I help you today? Ask about organs, blood compatibility, or how to register.",
    "hey": "Hey! I'm here to help with all your organ donation questions.",
    "good morning": "Good morning! 🌅 Ready to help you with organ donation queries.",
    "good evening": "Good evening! 🌙 Ask me anything about organ donation.",

    # Help menu
    "help": (
        "Here's what I can help with:<br/>"
        "🫀 <strong>Organs</strong> — heart, kidney, liver, lung, pancreas, intestine, cornea, bone marrow, skin<br/>"
        "🩸 <strong>Blood Groups</strong> — compatibility, universal donor/receiver<br/>"
        "📝 <strong>Registration</strong> — how to register as donor or receiver<br/>"
        "🔗 <strong>Matching</strong> — how matches are found<br/>"
        "📊 <strong>Stats</strong> — type 'stats' for live system counts<br/>"
        "❓ <strong>Myths</strong> — type 'myths' to bust common myths<br/>"
        "🚨 <strong>Urgency</strong> — type 'urgent' for organ viability times<br/>"
        "🌍 <strong>Impact</strong> — type 'impact' to know how many lives you can save"
    ),

    # Organs
    "organs": "We support 9 organs: Heart, Kidney, Liver, Lung, Pancreas, Intestine, Cornea, Bone Marrow, and Skin. Ask about any specific organ for details.",
    "heart": "❤️ <strong>Heart:</strong> Donated after brain death only. Must be transplanted within <strong>4–6 hours</strong>. Can save a life with heart failure.",
    "kidney": "🫘 <strong>Kidney:</strong> Most commonly donated organ. Living donors can donate one kidney. Viability: <strong>24–36 hours</strong>.",
    "liver": "🟤 <strong>Liver:</strong> Can regenerate — living donors can donate a portion. Viability: <strong>12–24 hours</strong>.",
    "lung": "🫁 <strong>Lung:</strong> Deceased donors can donate both lungs; living donors can donate a single lobe. Viability: <strong>4–6 hours</strong>.",
    "pancreas": "🟡 <strong>Pancreas:</strong> Helps patients with Type 1 diabetes. Viability: <strong>12–24 hours</strong>.",
    "intestine": "🔵 <strong>Intestine:</strong> Rare and complex donation. Helps patients with intestinal failure. Viability: <strong>8–16 hours</strong>.",
    "cornea": "👁️ <strong>Cornea:</strong> Can be donated by almost anyone regardless of blood group. Restores sight. Viability: <strong>up to 14 days</strong> with preservation.",
    "bone marrow": "🦴 <strong>Bone Marrow:</strong> Treats leukemia and blood disorders. Living donors can donate. Recovery takes a few weeks.",
    "skin": "🩹 <strong>Skin:</strong> Helps burn victims. Can be stored for <strong>up to 5 years</strong>. Donated after death.",

    # Blood groups
    "blood group": "We support all 8 blood groups: A+, A-, B+, B-, AB+, AB-, O+, O-. Compatibility is key for organ matching.",
    "blood": "🩸 Blood group compatibility is crucial for organ matching. O- is the universal donor; AB+ is the universal receiver.",
    "o negative": "🩸 <strong>O-</strong> is the universal donor — compatible with <strong>all blood types</strong>. Very valuable for donation.",
    "o positive": "🩸 <strong>O+</strong> can donate to O+, A+, B+, AB+. It's the most common blood group.",
    "ab positive": "🩸 <strong>AB+</strong> is the universal receiver — can receive organs from <strong>any blood group</strong>.",
    "ab negative": "🩸 <strong>AB-</strong> can receive from A-, B-, AB-, O-. Can donate to AB+ and AB-.",
    "a positive": "🩸 <strong>A+</strong> can donate to A+ and AB+. Can receive from A+, A-, O+, O-.",
    "b positive": "🩸 <strong>B+</strong> can donate to B+ and AB+. Can receive from B+, B-, O+, O-.",
    "compatible": (
        "🩸 <strong>Blood Compatibility Chart:</strong><br/>"
        "O- → All groups ✅<br/>"
        "O+ → O+, A+, B+, AB+<br/>"
        "A- → A-, A+, AB-, AB+<br/>"
        "A+ → A+, AB+<br/>"
        "B- → B-, B+, AB-, AB+<br/>"
        "B+ → B+, AB+<br/>"
        "AB- → AB-, AB+<br/>"
        "AB+ → AB+ only"
    ),

    # Registration
    "register": "📝 <strong>New user?</strong> Fill in your name, email, phone, age, blood group, city, state, gender, role and organ.<br/><strong>Already registered?</strong> Login with your email + password and add a new role/organ combo.",
    "multiple organs": "✅ Yes! You can register for multiple organs. After your first registration, login again with a different role/organ to add more entries.",
    "how to register": "Go to the <strong>Register</strong> page, fill in your details, choose your role (Donor/Receiver) and organ. That's it!",
    "donor": "🫀 A <strong>donor</strong> wishes to donate an organ after death or (for some organs) while living. Register with role 'Donor' and choose your organ.",
    "receiver": "🏥 A <strong>receiver</strong> needs an organ transplant. Register with role 'Receiver' and choose the organ you need.",
    "living donor": "💚 Living donors can donate: one kidney, part of the liver, a lung lobe, or bone marrow. It's a generous and life-saving act.",
    "deceased donor": "🕊️ Deceased donors can donate up to 8 organs and save multiple lives. Brain death is the usual criteria.",

    # Matching
    "match": "🔗 Matches are found based on: same organ type + blood group compatibility. Both parties get notified instantly when a match is found.",
    "matching": "🔗 Our system automatically matches donors and receivers by organ type and blood group compatibility. You'll get a notification when matched.",
    "notification": "🔔 When a match is found, both the donor and receiver get notified with each other's contact details so they can connect.",
    "how matching works": "The system checks: 1) Same organ needed/available, 2) Blood group compatibility. If both match, a notification is sent to both parties.",

    # Urgency / viability
    "urgent": (
        "🚨 <strong>Organ Viability Times (after removal):</strong><br/>"
        "Heart: 4–6 hours<br/>"
        "Lung: 4–6 hours<br/>"
        "Liver: 12–24 hours<br/>"
        "Pancreas: 12–24 hours<br/>"
        "Kidney: 24–36 hours<br/>"
        "Intestine: 8–16 hours<br/>"
        "Cornea: up to 14 days<br/>"
        "Bone Marrow: processed fresh<br/>"
        "Skin: up to 5 years (stored)"
    ),
    "viability": "⏱️ Organ viability varies: Heart/Lung (4–6 hrs), Liver/Pancreas (12–24 hrs), Kidney (24–36 hrs), Cornea (up to 14 days). Speed is critical!",
    "time": "⏱️ Time is critical in organ donation. Heart and lungs must be transplanted within 4–6 hours. Type 'urgent' for the full list.",

    # Impact
    "impact": "🌍 A single organ donor can save up to <strong>8 lives</strong> and improve the lives of over <strong>75 people</strong> through tissue donation.",
    "how many lives": "🌍 One donor can save up to <strong>8 lives</strong> through organ donation and help over <strong>75 people</strong> through tissue donation.",
    "save lives": "💪 By registering as a donor, you could save up to 8 lives. Every registration matters!",
    "statistics": "📊 Type <strong>stats</strong> to see live donor, receiver, and match counts from our system.",
    "stats": "live_stats",  # special key handled in get_bot_response

    # Myths
    "myths": (
        "❌ <strong>Common Organ Donation Myths — Busted!</strong><br/>"
        "1. <em>'Doctors won't save me if I'm a donor'</em> — FALSE. Saving your life is always the priority.<br/>"
        "2. <em>'My religion forbids it'</em> — Most major religions support donation as a gift of life.<br/>"
        "3. <em>'I'm too old to donate'</em> — There's no strict age limit; suitability is assessed medically.<br/>"
        "4. <em>'Rich people get organs faster'</em> — Matching is based on medical criteria, not wealth.<br/>"
        "5. <em>'My family will be charged'</em> — Donation costs nothing to the donor's family."
    ),
    "myth": "Type <strong>myths</strong> to see common organ donation myths debunked.",
    "religion": "🙏 Most major religions — including Christianity, Islam, Hinduism, Buddhism, and Judaism — support organ donation as an act of compassion and saving life.",
    "age limit": "👴 There is no strict age limit for organ donation. Even newborns and elderly people have donated organs. Medical suitability is assessed individually.",
    "cost": "💰 Organ donation is completely <strong>free</strong> for the donor's family. All medical costs related to donation are covered.",
    "safe": "✅ Organ donation is a safe, well-regulated medical procedure. Donors receive full medical care throughout the process.",

    # Process
    "process": (
        "📋 <strong>Donation Process:</strong><br/>"
        "1. Register on this platform as a donor<br/>"
        "2. System finds compatible receivers automatically<br/>"
        "3. Both parties are notified with contact details<br/>"
        "4. Connect and consult with medical professionals<br/>"
        "5. Medical team handles the transplant procedure"
    ),
    "how it works": "Register → System matches by organ + blood group → Both get notified → Connect → Medical team handles the rest. Type <strong>process</strong> for full details.",
    "steps": "Type <strong>process</strong> to see the step-by-step donation process.",

    # Admin
    "admin": "🔐 The admin panel lets administrators manage users, view all matches, and send notifications.",
    "admin login": "🔐 Admin credentials: Email: <strong>admin@organdonation.com</strong> | Password: <strong>admin123</strong>",

    # Closing
    "thank": "😊 You're welcome! Every question brings us closer to saving a life. Is there anything else I can help with?",
    "thanks": "😊 Happy to help! Feel free to ask anything else about organ donation.",
    "bye": "👋 Goodbye! Thank you for your interest in organ donation. Together we save lives!",
    "good bye": "👋 Take care! Remember — one donor can save 8 lives. 💙",
}


def get_bot_response(message):
    msg = message.lower().strip()

    # Special: live stats from DB
    if "stat" in msg or "count" in msg or "how many user" in msg or "how many donor" in msg or "how many receiver" in msg:
        donors    = db.session.query(Account.id).join(UserOrgan, Account.id == UserOrgan.account_id).filter(
            UserOrgan.role == 'donor', Account.is_admin == False).distinct().count()
        receivers = db.session.query(Account.id).join(UserOrgan, Account.id == UserOrgan.account_id).filter(
            UserOrgan.role == 'receiver', Account.is_admin == False).distinct().count()
        total     = Account.query.filter_by(is_admin=False).count()
        return (
            f"📊 <strong>Live System Stats:</strong><br/>"
            f"👥 Total Registered Users: <strong>{total}</strong><br/>"
            f"🫀 Donors: <strong>{donors}</strong><br/>"
            f"🏥 Receivers: <strong>{receivers}</strong><br/>"
            f"(Matches are calculated in real-time based on blood compatibility)"
        )

    # Check all keys
    for key, response in CHAT_RESPONSES.items():
        if key in msg:
            return response

    return (
        "🤔 I'm not sure about that. Try asking about:<br/>"
        "organs, blood groups, registration, matching, myths, urgent, stats, or impact.<br/>"
        "Type <strong>help</strong> to see all topics."
    )


@app.route('/chatbot', methods=['POST'])
def chatbot():
    data = request.get_json()
    user_message = data.get('message', '')
    if not user_message:
        return jsonify({'reply': 'Please type a message.'})
    return jsonify({'reply': get_bot_response(user_message)})


# ─── DB Init ──────────────────────────────────────────────────────────────────

def init_db():
    with app.app_context():
        db.create_all()
        # Remove orphaned notifications whose account_id doesn't match any account
        valid_ids = {a.id for a in Account.query.with_entities(Account.id).all()}
        orphaned = Notification.query.filter(
            ~Notification.account_id.in_(valid_ids)
        ).all() if valid_ids else Notification.query.all()
        for n in orphaned:
            db.session.delete(n)
        if orphaned:
            db.session.commit()
        if not Account.query.filter_by(email='admin@organdonation.com').first():
            admin_pw = bcrypt.generate_password_hash('admin123').decode('utf-8')
            admin = Account(
                name='Admin', email='admin@organdonation.com', password=admin_pw,
                phone='+10000000000', age=30, blood_group='O+',
                city='Admin City', state='Admin State', is_admin=True
            )
            db.session.add(admin)
            db.session.commit()
            print("Admin created: admin@organdonation.com / admin123")


init_db()

if __name__ == '__main__':
    app.run(debug=True)
