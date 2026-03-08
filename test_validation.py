"""Validation script: test all operations on the 2024 NSC question set."""
import os
import re
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'qems2.settings')
import django
django.setup()

from django.test import Client
from qems2.qsub.models import *
from django_comments.models import Comment
from django.contrib.contenttypes.models import ContentType

c = Client()
c.login(username='admin', password='admin')
qset_id = 3
qset = QuestionSet.objects.get(id=qset_id)
dist_entry = DistributionEntry.objects.filter(distribution=qset.distribution).first()
qt_tu = QuestionType.objects.get(question_type='ACF-style tossup')
qt_bo = QuestionType.objects.get(question_type='ACF-style bonus')
admin_writer = Writer.objects.get(user__username='admin')

results = []


# TEST 1: Load the 2024 NSC question set page
print("TEST 1: Load question set page")
resp = c.get(f'/edit_question_set/{qset_id}/')
if resp.status_code == 200 and '2024 NSC' in resp.content.decode():
    results.append(('Load question set', 'PASS'))
    print('  PASS')
else:
    results.append(('Load question set', f'FAIL (status {resp.status_code})'))
    print(f'  FAIL (status {resp.status_code})')


# TEST 2: Add a tossup
print("TEST 2: Add a tossup")
tu_count_before = Tossup.objects.filter(question_set=qset).count()
resp = c.post(f'/add_tossups/{qset_id}/', {
    'tossup_text': 'This is a test tossup about quantum mechanics. For 10 points, name this branch of physics.',
    'tossup_answer': '_quantum mechanics_ [accept _QM_]',
    'category': dist_entry.id,
    'author': admin_writer.id,
    'question_type': qt_tu.id,
    'edited': False,
    'locked': False,
    'proofread': False,
    'read_carefully': False,
})
tu_count_after = Tossup.objects.filter(question_set=qset).count()
if tu_count_after == tu_count_before + 1:
    new_tu = Tossup.objects.filter(question_set=qset).order_by('-id').first()
    results.append(('Add tossup', 'PASS'))
    print(f'  PASS (new ID: {new_tu.id})')
else:
    content = resp.content.decode()
    if 'Your tossup has been added' in content:
        results.append(('Add tossup', 'PASS'))
        print(f'  PASS (success message found, count {tu_count_before}->{tu_count_after})')
    else:
        results.append(('Add tossup', f'FAIL (count {tu_count_before}->{tu_count_after}, status {resp.status_code})'))
        print(f'  FAIL (count {tu_count_before}->{tu_count_after}, status {resp.status_code})')
        errs = re.findall(r'alert-box[^>]*>([^<]+)', content)
        for e in errs:
            print(f'    Alert: {e.strip()[:200]}')


# TEST 3: Add a bonus
print("TEST 3: Add a bonus")
bo_count_before = Bonus.objects.filter(question_set=qset).count()
resp = c.post(f'/add_bonuses/{qset_id}/ACF-style bonus/', {
    'leadin': 'Answer these questions about test bonuses. For 10 points each:',
    'part1_text': 'This is part 1 of the test bonus.',
    'part1_answer': '_answer 1_',
    'part2_text': 'This is part 2 of the test bonus.',
    'part2_answer': '_answer 2_',
    'part3_text': 'This is part 3 of the test bonus.',
    'part3_answer': '_answer 3_',
    'category': dist_entry.id,
    'author': admin_writer.id,
    'question_type': qt_bo.id,
    'edited': False,
    'locked': False,
    'proofread': False,
    'read_carefully': False,
})
bo_count_after = Bonus.objects.filter(question_set=qset).count()
if bo_count_after == bo_count_before + 1:
    new_bo = Bonus.objects.filter(question_set=qset).order_by('-id').first()
    results.append(('Add bonus', 'PASS'))
    print(f'  PASS (new ID: {new_bo.id})')
else:
    content = resp.content.decode()
    if 'Your bonus has been added' in content:
        results.append(('Add bonus', 'PASS'))
        print(f'  PASS (success message found, count {bo_count_before}->{bo_count_after})')
    else:
        results.append(('Add bonus', f'FAIL (count {bo_count_before}->{bo_count_after}, status {resp.status_code})'))
        print(f'  FAIL (count {bo_count_before}->{bo_count_after}, status {resp.status_code})')
        errs = re.findall(r'alert-box[^>]*>([^<]+)', content)
        for e in errs:
            print(f'    Alert: {e.strip()[:200]}')


# TEST 4: Edit a tossup
print("TEST 4: Edit a tossup")
tu = Tossup.objects.filter(question_set=qset).order_by('-id').first()
resp_get = c.get(f'/edit_tossup/{tu.id}/')
if resp_get.status_code != 200:
    results.append(('Edit tossup', f'FAIL GET (status {resp_get.status_code})'))
    print(f'  FAIL GET (status {resp_get.status_code})')
else:
    print(f'  GET OK')
    resp = c.post(f'/edit_tossup/{tu.id}/', {
        'tossup_text': tu.tossup_text + ' EDITED.',
        'tossup_answer': tu.tossup_answer,
        'category': dist_entry.id,
        'author': tu.author.id,
        'question_type': qt_tu.id,
        'edited': True,
        'locked': False,
        'proofread': False,
        'read_carefully': False,
        'change_type': 'question_submit',
    })
    tu.refresh_from_db()
    if 'EDITED.' in tu.tossup_text:
        results.append(('Edit tossup', 'PASS'))
        print(f'  PASS - tossup text updated')
    else:
        results.append(('Edit tossup', f'FAIL POST (status {resp.status_code})'))
        print(f'  FAIL - text not updated (status {resp.status_code})')


# TEST 5: Edit a bonus
print("TEST 5: Edit a bonus")
bo = Bonus.objects.filter(question_set=qset).order_by('-id').first()
resp_get = c.get(f'/edit_bonus/{bo.id}/')
if resp_get.status_code != 200:
    results.append(('Edit bonus', f'FAIL GET (status {resp_get.status_code})'))
    print(f'  FAIL GET (status {resp_get.status_code})')
else:
    print(f'  GET OK')
    resp = c.post(f'/edit_bonus/{bo.id}/', {
        'leadin': bo.leadin + ' EDITED.',
        'part1_text': bo.part1_text,
        'part1_answer': bo.part1_answer,
        'part2_text': bo.part2_text,
        'part2_answer': bo.part2_answer,
        'part3_text': bo.part3_text,
        'part3_answer': bo.part3_answer,
        'category': dist_entry.id,
        'author': bo.author.id,
        'question_type': qt_bo.id,
        'edited': True,
        'locked': False,
        'proofread': False,
        'read_carefully': False,
        'change_type': 'question_submit',
    })
    bo.refresh_from_db()
    if 'EDITED.' in bo.leadin:
        results.append(('Edit bonus', 'PASS'))
        print(f'  PASS - bonus leadin updated')
    else:
        results.append(('Edit bonus', f'FAIL POST (status {resp.status_code})'))
        print(f'  FAIL - leadin not updated (status {resp.status_code})')


# TEST 6: Add a comment to a tossup
print("TEST 6: Add a comment to a tossup")
tu = Tossup.objects.filter(question_set=qset).first()
comment_count_before = Comment.objects.count()

# Load edit page to get comment form security fields
resp_page = c.get(f'/edit_tossup/{tu.id}/')
if resp_page.status_code == 200:
    content = resp_page.content.decode()
    # Extract all hidden input values
    hidden = {}
    for m in re.finditer(r'<input[^>]*type=["\']hidden["\'][^>]*>', content):
        tag = m.group(0)
        name_m = re.search(r'name=["\']([^"\']*)["\']', tag)
        val_m = re.search(r'value=["\']([^"\']*)["\']', tag)
        if name_m and val_m:
            # Keep the last occurrence (the comment form one)
            hidden[name_m.group(1)] = val_m.group(1)

    if 'timestamp' in hidden and 'security_hash' in hidden:
        post_data = {
            'content_type': hidden.get('content_type', 'qsub.tossup'),
            'object_pk': hidden.get('object_pk', str(tu.pk)),
            'comment': 'This is a test comment on a tossup!',
            'timestamp': hidden['timestamp'],
            'security_hash': hidden['security_hash'],
        }
        resp = c.post('/comments/post/', post_data, follow=True)
        resp_content = resp.content.decode()
        if 'Thank you for your comment' in resp_content or 'Thanks for commenting' in resp_content:
            results.append(('Add comment', 'PASS'))
            print(f'  PASS - comment posted successfully')
        else:
            comment_count_after = Comment.objects.count()
            if comment_count_after > comment_count_before:
                results.append(('Add comment', 'PASS'))
                print(f'  PASS - comment added (count {comment_count_before}->{comment_count_after})')
            else:
                results.append(('Add comment', f'FAIL (status {resp.status_code})'))
                print(f'  FAIL (status {resp.status_code})')
                print(f'    Response snippet: {resp_content[:500]}')
    else:
        results.append(('Add comment', 'FAIL - could not extract form fields'))
        print(f'  FAIL - hidden fields found: {list(hidden.keys())}')
else:
    results.append(('Add comment', f'FAIL - edit page status {resp_page.status_code}'))
    print(f'  FAIL - edit page status {resp_page.status_code}')


# SUMMARY
print()
print('=' * 60)
print('SUMMARY')
print('=' * 60)
all_pass = True
for name, result in results:
    status = 'OK' if 'PASS' in result else 'XX'
    if status == 'XX':
        all_pass = False
    print(f'  [{status}] {name}: {result}')
print()
if all_pass:
    print('All tests passed!')
else:
    print('Some tests FAILED.')
    sys.exit(1)
