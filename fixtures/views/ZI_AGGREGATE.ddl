// R-03 + R-04 — aggregation and GROUP BY. Structurally impossible for CDC:
// a changed base row cannot be mapped back to an aggregated result row.
@EndUserText.label: 'Order value per customer'
@Analytics: { dataCategory: #FACT,
  dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_AGGREGATE
  as select from zcustorder
{
  key customer      as Customer,
      sum(amount)   as TotalAmount,
      count( * )    as OrderCount
}
group by customer
