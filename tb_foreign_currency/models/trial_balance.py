# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models, api, _, fields
from datetime import datetime, timedelta
from odoo.tools.misc import format_date

try:
    from odoo.tools.misc import xlsxwriter
except ImportError:
    # TODO saas-17: remove the try/except to directly import from misc
    import xlsxwriter
import io
from odoo.tools import DEFAULT_SERVER_DATE_FORMAT, pycompat



class report_account_coa(models.AbstractModel):
    _inherit = "account.coa.report"
    
    filter_currencys = True
        
    @api.model
    def _get_options(self, previous_options=None):
        res = super(report_account_coa, self)._get_options(previous_options)
        if self.filter_currencys :
            currencies = self.env['res.currency'].search([])
            res['currenciess'] = [{'id': c.id, 'name': c.name, 'selected': False} for c in currencies]
            if 'curr' in self._context:
                for c in res['currenciess']:
                    if c['id'] == self._context.get('curr'):
                        c['selected'] = True
            else:
                for c in res['currenciess']:
                    if c['id'] == self.env.user.company_id.currency_id.id:
                        c['selected'] = True
            res['currencys'] = True
        return res
    
    @api.model
    def _create_hierarchy(self, lines, options):
        """This method is called when the option 'hiearchy' is enabled on a report.
        It receives the lines (as computed by _get_lines()) in argument, and will add
        a hiearchy in those lines by using the account.group of accounts. If not set,
        it will fallback on creating a hierarchy based on the account's code first 3
        digits.
        """
        if 'curr' in self._context:
            is_number = ['number' in c.get('class', []) for c in self.get_header(options)[-1][1:]]
            # Avoid redundant browsing.
            accounts_cache = {}
    
            # Retrieve account either from cache, either by browsing.
            def get_account(id):
                if id not in accounts_cache:
                    accounts_cache[id] = self.env['account.account'].browse(id)
                return accounts_cache[id]
    
            # Add the report line to the hierarchy recursively.
            def add_line_to_hierarchy(line, codes, level_dict, depth=None):
                # Recursively build a dict where:
                # 'children' contains only subcodes
                # 'lines' contains the lines at this level
                # This > lines [optional, i.e. not for topmost level]
                #      > children > [codes] "That" > lines
                #                                  > metadata
                #                                  > children
                #      > metadata(depth, parent ...)
    
                if not codes:
                    return
                if not depth:
                    depth = line.get('level', 1)
                level_dict.setdefault('depth', depth)
                level_dict.setdefault('parent_id', 'hierarchy_' + codes[0][1] if codes[0][0] != 'root' else codes[0][1])
                level_dict.setdefault('children', {})
                code = codes[1]
                codes = codes[1:]
                level_dict['children'].setdefault(code, {})
    
                if len(codes) > 1:
                    add_line_to_hierarchy(line, codes, level_dict['children'][code], depth=depth + 1)
                else:
                    level_dict['children'][code].setdefault('lines', [])
                    level_dict['children'][code]['lines'].append(line)
                    for l in level_dict['children'][code]['lines']:
                        l['parent_id'] = 'hierarchy_' + code[1]
    
            # Merge a list of columns together and take care about str values.
            def merge_columns(columns):
                return [('n/a' if any(i != '' for i in x) else '') if any(isinstance(i, str) for i in x) else sum(x) for x in zip(*columns)]
    
            # Get_lines for the newly computed hierarchy.
            def get_hierarchy_lines(values, depth=1):
                lines = []
                sum_sum_columns = []
                unfold_all = self.env.context.get('print_mode') and len(options.get('unfolded_lines')) == 0
                for base_line in values.get('lines', []):
                    lines.append(base_line)
                    sum_sum_columns.append([c.get('no_format_name', c['name']) for c in base_line['columns']])
    
                # For the last iteration, there might not be the children key (see add_line_to_hierarchy)
                for key in sorted(values.get('children', {}).keys()):
                    sum_columns, sub_lines = get_hierarchy_lines(values['children'][key], depth=values['depth'])
                    id = 'hierarchy_' + key[1]
                    header_line = {
                        'id': id,
                        'name': key[1] if len(key[1]) < 30 else key[1][:30] + '...',  # second member of the tuple
                        'title_hover': key[1],
                        'unfoldable': True,
                        'unfolded': id in options.get('unfolded_lines') or unfold_all,
                        'level': values['depth'],
                        'parent_id': values['parent_id'],
                        'columns': [{'name': self.format_value(c) if not isinstance(c, str) else c} for c in sum_columns],
                    }
                    if key[0] == self.LEAST_SORT_PRIO:
                        header_line['style'] = 'font-style:italic;'
                    lines += [header_line] + sub_lines
                    sum_sum_columns.append(sum_columns)
                return merge_columns(sum_sum_columns), lines
    
            def deep_merge_dict(source, destination):
                for key, value in source.items():
                    if isinstance(value, dict):
                        # get node or create one
                        node = destination.setdefault(key, {})
                        deep_merge_dict(value, node)
                    else:
                        destination[key] = value
    
                return destination
    
            # Hierarchy of codes.
            accounts_hierarchy = {}
    
            new_lines = []
            no_group_lines = []
            # If no account.group at all, we need to pass once again in the loop to dispatch
            # all the lines across their account prefix, hence the None
            for line in lines + [None]:
                # Only deal with lines grouped by accounts.
                # And discriminating sections defined by account.financial.html.report.line
                is_grouped_by_account = line and line.get('caret_options') == 'account.account'
                if not is_grouped_by_account or not line:
    
                    # No group code found in any lines, compute it automatically.
                    no_group_hierarchy = {}
                    for no_group_line in no_group_lines:
                        codes = [('root', str(line.get('parent_id')) or 'root'), (self.LEAST_SORT_PRIO, _('(No Group)'))]
                        if not accounts_hierarchy:
                            account = get_account(no_group_line.get('account_id', no_group_line.get('id')))
                            codes = [('root', str(line.get('parent_id')) or 'root')] + self.get_account_codes(account)
                        add_line_to_hierarchy(no_group_line, codes, no_group_hierarchy, line.get('level', 0) + 1)
                    no_group_lines = []
    
                    deep_merge_dict(no_group_hierarchy, accounts_hierarchy)
    
                    # Merge the newly created hierarchy with existing lines.
                    if accounts_hierarchy:
                        new_lines += get_hierarchy_lines(accounts_hierarchy)[1]
                        accounts_hierarchy = {}
    
                    if line:
                        new_lines.append(line)
                    continue
    
                # Exclude lines having no group.
                account = get_account(line.get('account_id', line.get('id')))
                if not account.group_id:
                    no_group_lines.append(line)
                    continue
    
                codes = [('root', str(line.get('parent_id')) or 'root')] + self.get_account_codes(account)
                add_line_to_hierarchy(line, codes, accounts_hierarchy, line.get('level', 0) + 1)
    
            return new_lines
        return super(report_account_coa, self)._create_hierarchy(lines, options)
#         """This method is called when the option 'hiearchy' is enabled on a report.
#         It receives the lines (as computed by get_lines()) in argument, and will add
#         a hiearchy in those lines by using the account.group of accounts. If not set,
#         it will fallback on creating a hierarchy based on the account's code first 3
#         digits.
#         """
#         # Avoid redundant browsing.
#         if 'curr' in self._context:
#             cur = self.env['res.currency'].browse(self._context.get('curr'))
#             if cur != self.env.user.company_id.currency_id:
#                 accounts_cache = {}
#         
#                 MOST_SORT_PRIO = 0
#                 LEAST_SORT_PRIO = 99
#         
#                 # Retrieve account either from cache, either by browsing.
#                 def get_account(id):
#                     if id not in accounts_cache:
#                         accounts_cache[id] = self.env['account.account'].browse(id)
#                     return accounts_cache[id]
#         
#                 # Create codes path in the hierarchy based on account.
#                 def get_account_codes(account):
#                     # A code is tuple(sort priority, actual code)
#                     codes = []
#                     if account.group_id:
#                         group = account.group_id
#                         while group:
#                             code = '%s %s' % (group.code_prefix or '', group.name)
#                             codes.append((MOST_SORT_PRIO, code))
#                             group = group.parent_id
#                     else:
#                         # Limit to 3 levels.
#                         code = account.code[:3]
#                         while code:
#                             codes.append((MOST_SORT_PRIO, code))
#                             code = code[:-1]
#                     return list(reversed(codes))
#         
#                 # Add the report line to the hierarchy recursively.
#                 def add_line_to_hierarchy(line, codes, level_dict, depth=None):
#                     # Recursively build a dict where:
#                     # 'children' contains only subcodes
#                     # 'lines' contains the lines at this level
#                     # This > lines [optional, i.e. not for topmost level]
#                     #      > children > [codes] "That" > lines
#                     #                                  > metadata
#                     #                                  > children
#                     #      > metadata(depth, parent ...)
#         
#                     if not codes:
#                         return
#                     if not depth:
#                         depth = line.get('level', 1)
#                     level_dict.setdefault('depth', depth)
#                     level_dict.setdefault('parent_id', line.get('parent_id'))
#                     level_dict.setdefault('children', {})
#                     code = codes[0]
#                     codes = codes[1:]
#                     level_dict['children'].setdefault(code, {})
#         
#                     if codes:
#                         add_line_to_hierarchy(line, codes, level_dict['children'][code], depth=depth + 1)
#                     else:
#                         level_dict['children'][code].setdefault('lines', [])
#                         level_dict['children'][code]['lines'].append(line)
#         
#                 # Merge a list of columns together and take care about str values.
#                 def merge_columns(columns):
#                     return ['n/a' if any(isinstance(i, str) for i in x) else sum(x) for x in pycompat.izip(*columns)]
#         
#                 # Get_lines for the newly computed hierarchy.
#                 def get_hierarchy_lines(values, depth=1):
#                     lines = []
#                     sum_sum_columns = []
#                     for base_line in values.get('lines', []):
#                         lines.append(base_line)
#                         sum_sum_columns.append([c.get('no_format_name', c['name']) for c in base_line['columns']])
#         
#                     # For the last iteration, there might not be the children key (see add_line_to_hierarchy)
#                     for key in sorted(values.get('children', {}).keys()):
#                         sum_columns, sub_lines = get_hierarchy_lines(values['children'][key], depth=values['depth'])
#                         header_line = {
#                             'id': 'hierarchy',
#                             'name': key[1],  # second member of the tuple
#                             'unfoldable': False,
#                             'unfolded': True,
#                             'level': values['depth'],
#                             'parent_id': values['parent_id'],
#                             'columns': [{'name': self.format_value(c,cur) if not isinstance(c, str) else c} for c in sum_columns],
#                         }
#                         if key[0] == LEAST_SORT_PRIO:
#                             header_line['style'] = 'font-style:italic;'
#                         lines += [header_line] + sub_lines
#                         sum_sum_columns.append(sum_columns)
#                     return merge_columns(sum_sum_columns), lines
#         
#                 def deep_merge_dict(source, destination):
#                     for key, value in source.items():
#                         if isinstance(value, dict):
#                             # get node or create one
#                             node = destination.setdefault(key, {})
#                             deep_merge_dict(value, node)
#                         else:
#                             destination[key] = value
#         
#                     return destination
#         
#                 # Hierarchy of codes.
#                 accounts_hierarchy = {}
#         
#                 new_lines = []
#                 no_group_lines = []
#                 # If no account.group at all, we need to pass once again in the loop to dispatch
#                 # all the lines across their account prefix, hence the None
#                 for line in lines + [None]:
#                     # Only deal with lines grouped by accounts.
#                     # And discriminating sections defined by account.financial.html.report.line
#                     is_grouped_by_account = line and line.get('caret_options') == 'account.account'
#                     if not is_grouped_by_account or not line:
#         
#                         # No group code found in any lines, compute it automatically.
#                         no_group_hierarchy = {}
#                         for no_group_line in no_group_lines:
#                             codes = [(LEAST_SORT_PRIO, _('(No Group)'))]
#                             if not accounts_hierarchy:
#                                 account = get_account(no_group_line.get('id'))
#                                 codes = get_account_codes(account)
#                             add_line_to_hierarchy(no_group_line, codes, no_group_hierarchy)
#                         no_group_lines = []
#         
#                         deep_merge_dict(no_group_hierarchy, accounts_hierarchy)
#         
#                         # Merge the newly created hierarchy with existing lines.
#                         if accounts_hierarchy:
#                             new_lines += get_hierarchy_lines(accounts_hierarchy)[1]
#                             accounts_hierarchy = {}
#         
#                         if line:
#                             new_lines.append(line)
#                         continue
#         
#                     # Exclude lines having no group.
#                     account = get_account(line.get('id'))
#                     if not account.group_id:
#                         no_group_lines.append(line)
#                         continue
#         
#                     codes = get_account_codes(account)
#                     add_line_to_hierarchy(line, codes, accounts_hierarchy)
#         
#                 return new_lines
#         return super(report_account_coa, self)._create_hierarchy(lines)
    
    @api.model
    def _get_lines(self, options, line_id=None):
        if self._context.get('curr',False):
            cur = self.env['res.currency'].browse(self._context.get('curr'))
            new_options = options.copy()
            new_options['unfold_all'] = True
            options_list = self._get_options_periods_list(new_options)
            accounts_results, taxes_results = self.env['account.general.ledger']._do_query(options_list, fetch_lines=False)
    
            lines = []
            totals = [0.0] * (2 * (len(options_list) + 2))
    
            # Add lines, one per account.account record.
            for account, periods_results in accounts_results:
                sums = []
                account_balance = 0.0
                for i, period_values in enumerate(reversed(periods_results)):
                    account_sum = period_values.get('sum', {})
                    account_un_earn = period_values.get('unaffected_earnings', {})
                    account_init_bal = period_values.get('initial_balance', {})
    
                    if i == 0:
                        # Append the initial balances.
                        initial_balance = account_init_bal.get('balance', 0.0) + account_un_earn.get('balance', 0.0)
                        initial_balance = cur._compute(self.env.user.company_id.currency_id,cur,initial_balance)
                        sums += [
                            initial_balance > 0 and initial_balance or 0.0,
                            initial_balance < 0 and -initial_balance or 0.0,
                        ]
                        account_balance += initial_balance
    
                    # Append the debit/credit columns.
                    debit = cur._compute(self.env.user.company_id.currency_id,cur,account_sum.get('debit', 0.0))
                    credit = cur._compute(self.env.user.company_id.currency_id,cur,account_sum.get('credit', 0.0))
                    
                    debit1 = cur._compute(self.env.user.company_id.currency_id,cur,account_init_bal.get('debit', 0.0))
                    credit1 = cur._compute(self.env.user.company_id.currency_id,cur,account_init_bal.get('credit', 0.0))
                    sums += [
                        debit - debit1,
                        credit - credit1,
                    ]
                    account_balance += sums[-2] - sums[-1]
    
                # Append the totals.
                sums += [
                    account_balance > 0 and account_balance or 0.0,
                    account_balance < 0 and -account_balance or 0.0,
                ]
    
                # account.account report line.
                columns = []
                for i, value in enumerate(sums):
                    # Update totals.
                    totals[i] += value
    
                    # Create columns.
                    columns.append({'name': self.format_value(value, blank_if_zero=True,currency=cur), 'class': 'number', 'no_format_name': value})
    
                name = account.name_get()[0][1]
                if len(name) > 40 and not self._context.get('print_mode'):
                    name = name[:40]+'...'
    
                lines.append({
                    'id': account.id,
                    'name': name,
                    'title_hover': name,
                    'columns': columns,
                    'unfoldable': False,
                    'caret_options': 'account.account',
                })
            for t in totals:
                t = cur._compute(self.env.user.company_id.currency_id,cur,t)
    
            # Total report line.
            lines.append({
                 'id': 'grouped_accounts_total',
                 'name': _('Total'),
                 'class': 'total',
                 'columns': [{'name': self.format_value(total,currency=cur), 'class': 'number'} for total in totals],
                 'level': 1,
            })
    
            return lines
        return super(report_account_coa, self)._get_lines(options, line_id)
    
    def get_pdf(self, options, minimal_layout=True):
        for opt in options['currenciess']:
            if opt['selected'] and self.env['res.currency'].browse(opt['id']) != self.env.user.company_id.currency_id:
                return super(report_account_coa, self.with_context(curr = opt['id'])).get_pdf(options,minimal_layout)
        return super(report_account_coa, self).get_pdf(options,minimal_layout)
    
    def get_xlsx(self, options, response=None):
        for opt in options['currenciess']:
            if opt['selected'] and self.env['res.currency'].browse(opt['id']) != self.env.user.company_id.currency_id:
                return super(report_account_coa, self.with_context(curr = opt['id'])).get_xlsx(options,response)
        return super(report_account_coa, self).get_xlsx(options,response)

   