// R-05 — HAVING (and therefore aggregation).
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_HAVING
  as select from zorderitem
{
  key orderid        as OrderId,
      sum(quantity)  as TotalQuantity
}
group by orderid
having sum(quantity) > 0
