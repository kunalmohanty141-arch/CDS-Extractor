-- Parser fixture, from a real failure against SAP's I_MaterialDocumentRecord.
--
-- ABAP CDS DDL accepts '--' as a line comment as well as '//'. SAP's delivered
-- content uses it heavily. Missing it is not cosmetic: an apostrophe inside
-- such a comment -- as in "we don't use any key fields at all" -- opens a
-- string literal that never closes, and the entire view becomes UNPARSEABLE.
--
-- Traps below: a commented-out group by, and an apostrophe in prose.
--   group by customer
-- we don't want that clause, and the parser mustn't see it
@EndUserText.label: 'Dash comment traps'
@Analytics: { dataCategory: #FACT,
  dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_DASH_COMMENTS
  as select from zcustorder
{
  key orderid   as OrderId,     -- the main key; don't remove it
      customer  as Customer,    -- customer's identifier
      amount    as Amount
      -- distinct, union all, having -- none of these are code
}
