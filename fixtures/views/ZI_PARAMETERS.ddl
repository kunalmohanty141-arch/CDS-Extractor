// R-08 — view parameters. Note 2890171; Replication Flows cannot supply a value.
@Analytics: { dataExtraction: { enabled: true,
    delta.changeDataCapture.automatic: true } }
define view entity ZI_PARAMETERS
  with parameters
    p_from_date : abap.dats,
    p_currency  : abap.cuky(5)
  as select from zcustorder
{
  key orderid   as OrderId,
      orderdate as OrderDate,
      amount    as Amount
}
where orderdate >= $parameters.p_from_date
